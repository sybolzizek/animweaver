import asyncio
import os

import pytest
from PIL import Image

from test_stdio import call, connect


def test_two_layer_seam_follows_mesh_and_rigid_cutouts(tmp_path):
    if not (
        os.environ.get("REFERENCE_BROWSER_DEPS")
        and os.environ.get("REFERENCE_BROWSER_EXECUTABLE")
    ):
        pytest.skip("Set browser integration paths")
    bg = (30, 60, 100)
    cloth = (190, 70, 42)
    cover = (26, 175, 70)
    for key, size, fill in (
        ("back", (110, 72), bg),
        ("top", (20, 18), cloth),
        ("bottom", (20, 18), cloth),
        ("cover", (3, 19), cover),
    ):
        Image.new("RGB", size, fill).save(tmp_path / f"{key}.png")

    async def run():
        async with connect(tmp_path) as session:
            ids = {
                k: (await call(session, "register_original", path=f"{k}.png"))["id"]
                for k in ("back", "top", "bottom", "cover")
            }
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=ids["back"],
                duration=2,
                groups=[
                    {
                        "key": "Shift",
                        "motion": [
                            {"time": 0},
                            {"time": 1, "move": [8, 10]},
                            {"time": 2, "move": [10, 30]},
                        ],
                    }
                ],
                layers=[
                    {
                        "key": "Top",
                        "source": ids["top"],
                        "offset": [30, 12],
                        "motion": [
                            {"time": 0},
                            {"time": 1, "rotate": 12, "move": [3, -5]},
                        ],
                        "pivot": [40, 30],
                    },
                    {
                        "key": "Bottom",
                        "source": ids["bottom"],
                        "offset": [30, 30],
                        "mesh": {"bones": [{"key": "Shift"}], "cell": 5},
                    },
                    {"key": "Occluder", "source": ids["cover"], "offset": [44, 24]},
                ],
            )
            seam = {
                "key": "Joint",
                "a": {"layer": "Top", "rail": [[2, 16], [17, 16]]},
                "b": {"layer": "Bottom", "rail": [[2, 2], [17, 2]]},
                "max_span": 25,
            }
            joined = await call(
                session, "revise_cutout_scene", id=scene["id"], seams=[seam]
            )
            assert "cutout-seams.mjs" in joined["files"]
            assert joined["cutouts"] == scene["cutouts"]
            assert "seam-Joint.png" in joined["files"]
            assert await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                seams=[
                    seam | {"a": {"layer": "Top", "rail": [[0, 7], [0, 13]]}},
                ],
                fail=True,
            )
            for artifact, t, path in (
                (scene, 1, "gap.png"),
                (joined, 1, "joined.png"),
                (joined, 2, "limit.png"),
            ):
                output = await call(
                    session,
                    "render_cutout_scene",
                    id=artifact["id"],
                    time=t,
                    output=path,
                )
                if artifact is joined:
                    assert output["seam_report"][0]["status"] == (
                        "drawn" if t == 1 else "too_wide"
                    )
            with (
                Image.open(tmp_path / "gap.png") as before,
                Image.open(tmp_path / "joined.png") as after,
            ):
                assert before.getpixel((42, 32))[:3] == bg
                assert after.getpixel((42, 32))[:3] == cloth
                assert after.getpixel((45, 32))[:3] == cover
                assert after.getpixel((55, 33))[:3] == bg
            with Image.open(tmp_path / "limit.png") as after:
                assert after.getpixel((42, 36))[:3] == bg
            # Reversed projected correspondence is a compact way to force a crossing
            # inside a render cell. The rest of the connector must survive.
            crossed = await call(
                session,
                "revise_cutout_scene",
                id=joined["id"],
                seams=[
                    seam
                    | {
                        "max_span": 40,
                        "b": {"layer": "Bottom", "rail": [[17, 2], [2, 2]]},
                    }
                ],
            )
            frame = await call(
                session,
                "render_cutout_scene",
                id=crossed["id"],
                time=1,
                output="crossed.png",
            )
            status = frame["seam_report"][0]
            assert status["status"] == "drawn" and status["crossings"] > 0
            cleared = await call(
                session, "revise_cutout_scene", id=joined["id"], seams=[]
            )
            assert "seam-Joint.png" not in cleared["files"]

    asyncio.run(run())
