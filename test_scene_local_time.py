import asyncio
import os

import numpy as np
import pytest
from PIL import Image

from test_stdio import call, connect


def _browser_ready():
    return os.environ.get("REFERENCE_BROWSER_DEPS") and os.environ.get(
        "REFERENCE_BROWSER_EXECUTABLE"
    )


def test_local_time_revision_reuses_rasters_and_restores_global_validation(tmp_path):
    Image.new("RGB", (80, 40), (3, 5, 7)).save(tmp_path / "plate.png")
    Image.new("RGBA", (6, 6), (220, 30, 20, 255)).save(tmp_path / "part.png")

    async def run():
        async with connect(tmp_path) as session:
            plate, part = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "part.png")
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                groups=[
                    {
                        "key": "Clock",
                        "local_time": {"scale": 2},
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 2, "move": [20, 0]},
                        ],
                    }
                ],
                layers=[
                    {
                        "key": "Part",
                        "source": part["id"],
                        "parent": "Clock",
                        "offset": [8, 8],
                    }
                ],
            )
            assert scene["groups"][0]["local_time"] == {
                "start": 0,
                "offset": 0,
                "scale": 2,
                "duration": None,
                "mode": "free",
            }
            assert "scene-evaluate.mjs" in scene["files"]

            source = tmp_path / part["files"]["image.png"]
            unavailable = source.with_suffix(".unavailable")
            source.rename(unavailable)
            try:
                revised = await call(
                    session,
                    "revise_cutout_scene",
                    id=scene["id"],
                    local_times={
                        "Clock": {
                            "start": 0.25,
                            "offset": 0,
                            "scale": 2,
                            "duration": 2,
                            "mode": "repeat",
                        }
                    },
                )
            finally:
                unavailable.rename(source)
            assert revised["groups"][0]["local_time"]["mode"] == "repeat"
            for name in ("cutout-Part.png", "background.png"):
                assert (tmp_path / revised["files"][name]).read_bytes() == (
                    tmp_path / scene["files"][name]
                ).read_bytes()

            # Removing the only local clock makes the key at t=2 invalid for
            # a one-second scene again.
            await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                local_times={"Clock": None},
                fail=True,
            )
            await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                groups=[{"key": "Broken", "local_time": {"mode": "repeat"}}],
                layers=[{"key": "Part", "source": part["id"], "parent": "Broken"}],
                fail=True,
            )

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_nested_repeat_reverse_freeze_and_pingpong_render_in_browser(tmp_path):
    Image.new("RGB", (90, 55), (0, 0, 0)).save(tmp_path / "plate.png")
    for name, color in (
        ("red.png", (255, 0, 0, 255)),
        ("green.png", (0, 255, 0, 255)),
        ("blue.png", (0, 0, 255, 255)),
        ("yellow.png", (255, 255, 0, 255)),
    ):
        Image.new("RGBA", (4, 4), color).save(tmp_path / name)

    async def run():
        async with connect(tmp_path) as session:
            plate, red, green, blue, yellow = [
                await call(session, "register_original", path=name)
                for name in (
                    "plate.png",
                    "red.png",
                    "green.png",
                    "blue.png",
                    "yellow.png",
                )
            ]
            linear_x = [
                {"time": 0, "ease": "linear"},
                {"time": 1, "move": [20, 0]},
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1.5,
                groups=[
                    {
                        "key": "Cycle",
                        "local_time": {
                            "scale": 2,
                            "duration": 2,
                            "mode": "repeat",
                        },
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 2, "move": [20, 0]},
                        ],
                    },
                    {
                        "key": "Reverse",
                        "local_time": {
                            "offset": 1,
                            "scale": -1,
                            "duration": 1,
                            "mode": "clamp",
                        },
                        "motion": linear_x,
                    },
                    {
                        "key": "Bounce",
                        "local_time": {
                            "scale": 2,
                            "duration": 1,
                            "mode": "pingpong",
                        },
                        "motion": linear_x,
                    },
                    {
                        "key": "Freeze",
                        "local_time": {"offset": 0.75, "scale": 0},
                        "motion": linear_x,
                    },
                ],
                layers=[
                    {
                        "key": "Red",
                        "source": red["id"],
                        "parent": "Cycle",
                        "offset": [5, 5],
                        "local_time": {"scale": 0.5},
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [0, 10]},
                        ],
                    },
                    {
                        "key": "Green",
                        "source": green["id"],
                        "parent": "Reverse",
                        "offset": [40, 5],
                    },
                    {
                        "key": "Blue",
                        "source": blue["id"],
                        "parent": "Bounce",
                        "offset": [5, 30],
                    },
                    {
                        "key": "Yellow",
                        "source": yellow["id"],
                        "parent": "Freeze",
                        "offset": [40, 30],
                    },
                ],
            )
            frames = {}
            for label, time in (("a", 0.25), ("b", 0.75), ("c", 1.25)):
                filename = f"local-{label}.png"
                await call(
                    session,
                    "render_cutout_scene",
                    id=scene["id"],
                    output=filename,
                    time=time,
                )
                frames[label] = np.asarray(Image.open(tmp_path / filename).convert("RGB"))

            def center(pixels, channel, other_a, other_b):
                found = np.argwhere(
                    (pixels[..., channel] > 220)
                    & (pixels[..., other_a] < 40)
                    & (pixels[..., other_b] < 40)
                )
                return found[:, 1].mean(), found[:, 0].mean()

            red_a = center(frames["a"], 0, 1, 2)
            red_b = center(frames["b"], 0, 1, 2)
            red_c = center(frames["c"], 0, 1, 2)
            np.testing.assert_allclose(red_a, (11.5, 9), atol=0.6)
            np.testing.assert_allclose(red_b, (21.5, 14), atol=0.6)
            np.testing.assert_allclose(red_c, red_a, atol=0.1)

            green_a = center(frames["a"], 1, 0, 2)
            green_b = center(frames["b"], 1, 0, 2)
            np.testing.assert_allclose(green_a[0], 56.5, atol=0.6)
            np.testing.assert_allclose(green_b[0], 46.5, atol=0.6)

            blue_a = center(frames["a"], 2, 0, 1)
            blue_b = center(frames["b"], 2, 0, 1)
            np.testing.assert_allclose(blue_a[0], 16.5, atol=0.6)
            np.testing.assert_allclose(blue_b[0], blue_a[0], atol=0.1)

            yellow_centers = []
            for label in ("a", "b", "c"):
                found = np.argwhere(
                    (frames[label][..., 0] > 220)
                    & (frames[label][..., 1] > 220)
                    & (frames[label][..., 2] < 40)
                )
                yellow_centers.append((found[:, 1].mean(), found[:, 0].mean()))
            np.testing.assert_allclose(yellow_centers, [(56.5, 31.5)] * 3, atol=0.1)

    asyncio.run(run())
