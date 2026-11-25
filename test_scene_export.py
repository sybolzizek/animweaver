import asyncio
import os
import shutil

import cv2
import pytest
from PIL import Image, ImageDraw

from test_stdio import call, connect


async def finish_job(session, job):
    async with asyncio.timeout(30):
        while True:
            state = await call(session, "inspect_cutout_video", id=job["id"])
            if state["status"] not in ("queued", "running"):
                return state
            await asyncio.sleep(0.1)


def test_multiple_images_to_preview_and_video(tmp_path):
    if not os.environ.get("REFERENCE_BROWSER_DEPS") or not os.environ.get(
        "REFERENCE_BROWSER_EXECUTABLE"
    ):
        pytest.skip("Set browser integration paths")
    Image.new("RGB", (121, 81), (30, 65, 80)).save(tmp_path / "background.png")
    Image.new("RGB", (24, 24), (235, 40, 20)).save(tmp_path / "square.png")
    dot = Image.new("RGBA", (20, 20), (10, 230, 50, 0))
    ImageDraw.Draw(dot).rectangle((5, 5, 14, 14), fill=(10, 230, 50, 255))
    dot.save(tmp_path / "dot.png")

    async def run():
        async with connect(tmp_path) as session:
            base, square, dot = [
                await call(session, "register_original", path=name)
                for name in ("background.png", "square.png", "dot.png")
            ]
            split = await call(session, "split_image", id=square["id"])
            tree = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[
                    {"key": "Square", "polygon": [[2, 2], [21, 2], [21, 21], [2, 21]]},
                ],
            )
            selected = tree["children"][0]["selection"]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=base["id"],
                duration=0.75,
                layers=[
                    {
                        "key": "Square",
                        "selection": selected,
                        "offset": [32, 21],
                        "motion": [{"time": 0}, {"time": 0.5, "move": [24, 0]}],
                    },
                    {"key": "Dot", "source": dot["id"], "offset": [40, 25]},
                ],
                groups=[{"key": "Joint", "pivot": [34, 23]}],
            )
            assert scene["cutouts"][0]["source_box"] == [2, 2, 22, 22]
            assert scene["cutouts"][0]["box"] == [34, 23, 54, 43]
            assert len(scene["inputs"]) == 3
            edited = await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                meshes={"Square": {"bones": [{"key": "Joint", "selection": selected}]}},
                depth={"Dot": [{"time": 0.5, "z": -1}]},
            )
            assert edited["parent"] == scene["id"] and edited["cutouts"][0]["mesh"][
                "bones"
            ] == ["Joint"]
            await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                fail=True,
                meshes={
                    "Square": {"bones": [{"key": "Joint", "selection": dot["id"]}]}
                },
            )
            for t, path in [(0, "start.png"), (0.5, "end.png")]:
                png = await call(
                    session, "render_cutout_scene", id=edited["id"], output=path, time=t
                )
                assert png["parent"] == edited["id"] and png["frames"] == 1
                assert (tmp_path / png["files"]["image.png"]).exists()
            with Image.open(tmp_path / "start.png") as frame:
                assert frame.getpixel((38, 30))[:3] == (235, 40, 20)
                assert frame.getpixel((46, 34))[:3] == (10, 230, 50)
            with Image.open(tmp_path / "end.png") as frame:
                assert frame.getpixel((38, 30))[:3] == (30, 65, 80)
                assert frame.getpixel((66, 34))[:3] == (235, 40, 20)
            await call(
                session,
                "render_cutout_scene",
                id=edited["id"],
                output="start.png",
                fail=True,
            )
            if shutil.which("ffmpeg"):
                job = await call(
                    session,
                    "render_cutout_scene",
                    id=edited["id"],
                    output="./clip.mp4",
                    fps=8,
                )
                assert job["output"] == "clip.mp4"
                state = await finish_job(session, job)
                assert state["status"] == "completed", state
                mp4 = state["render"]
                assert (mp4["width"], mp4["height"], mp4["frames"]) == (122, 82, 6)
                capture = cv2.VideoCapture(str(tmp_path / "clip.mp4"))
                try:
                    assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 6
                    assert capture.read()[1].shape[:2] == (82, 122)
                finally:
                    capture.release()
                transparent = await call(
                    session,
                    "prepare_cutout_scene",
                    id=dot["id"],
                    duration=0.5,
                    layers=[{"key": "Dot", "source": dot["id"]}],
                )
                rejected = await call(
                    session,
                    "render_cutout_scene",
                    id=transparent["id"],
                    output="no-black-fill.mp4",
                )
                failure = await finish_job(session, rejected)
                assert failure["status"] == "failed"
                assert "non-opaque pixels" in failure["error"]
                assert not (tmp_path / "no-black-fill.mp4").exists()
        if shutil.which("ffmpeg"):
            # A new MCP process can resume observation of the same render.
            async with connect(tmp_path) as session:
                again = await call(
                    session,
                    "render_cutout_scene",
                    id=edited["id"],
                    output="clip.mp4",
                    fps=8,
                )
                assert again["id"] == job["id"]
                assert (
                    await call(session, "inspect_cutout_video", id=job["id"]) == state
                )

    asyncio.run(run())
