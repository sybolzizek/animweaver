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


def test_compositing_and_projection_are_revisable_without_raster_rebuild(tmp_path):
    Image.new("RGB", (100, 60), (30, 40, 50)).save(tmp_path / "plate.png")
    Image.new("RGBA", (12, 12), (180, 90, 30, 255)).save(tmp_path / "part.png")

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
                layers=[
                    {
                        "key": "Part",
                        "source": part["id"],
                        "offset": [44, 24],
                        "plane_depth": 40,
                        "blend_mode": "screen",
                        "opacity": [
                            {"time": 0, "value": 0, "ease": "linear"},
                            {"time": 1, "value": 1},
                        ],
                    }
                ],
                camera={
                    "focal": 400,
                    "motion": [
                        {"time": 0},
                        {"time": 1, "truck": [10, 0], "dolly": 20},
                    ],
                },
            )
            assert scene["layers"][0]["blend_mode"] == "screen"
            assert scene["layers"][0]["plane_depth"] == 40
            assert scene["layers"][0]["opacity"][0]["value"] == 0

            source = tmp_path / part["files"]["image.png"]
            unavailable = source.with_suffix(".unavailable")
            source.rename(unavailable)
            try:
                revised = await call(
                    session,
                    "revise_cutout_scene",
                    id=scene["id"],
                    opacities={"Part": [{"time": 0, "value": 0.5}]},
                    blend_modes={"Part": "multiply"},
                    plane_depths={"Part": 80},
                )
            finally:
                unavailable.rename(source)
            assert revised["parent"] == scene["id"]
            assert revised["layers"][0]["blend_mode"] == "multiply"
            assert revised["layers"][0]["plane_depth"] == 80
            assert revised["layers"][0]["opacity"][0]["value"] == 0.5
            assert (tmp_path / revised["files"]["cutout-Part.png"]).read_bytes() == (
                tmp_path / scene["files"]["cutout-Part.png"]
            ).read_bytes()

            await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                plane_depths={"Part": 399},
                camera={"focal": 400},
                fail=True,
            )

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_opacity_blend_and_depth_parallax_render_in_browser(tmp_path):
    Image.new("RGB", (60, 40), (40, 60, 80)).save(tmp_path / "plate.png")
    Image.new("RGBA", (10, 10), (100, 120, 140, 255)).save(
        tmp_path / "overlay.png"
    )
    Image.new("RGB", (200, 100), (5, 5, 5)).save(tmp_path / "stage.png")
    Image.new("RGBA", (10, 10), (230, 20, 20, 255)).save(tmp_path / "far.png")
    Image.new("RGBA", (10, 10), (20, 230, 20, 255)).save(
        tmp_path / "near.png"
    )

    async def run():
        async with connect(tmp_path) as session:
            plate, overlay = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "overlay.png")
            ]
            composite = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                layers=[
                    {
                        "key": "Overlay",
                        "source": overlay["id"],
                        "offset": [20, 15],
                        "blend_mode": "screen",
                        "opacity": [
                            {"time": 0, "value": 0, "ease": "linear"},
                            {"time": 1, "value": 1},
                        ],
                    }
                ],
            )
            await call(
                session,
                "render_cutout_scene",
                id=composite["id"],
                output="composite.png",
                time=0.5,
            )
            with Image.open(tmp_path / "composite.png") as image:
                pixel = np.array(image.getpixel((24, 19))[:3])
            base = np.array([40, 60, 80], dtype=float)
            source = np.array([100, 120, 140], dtype=float)
            screened = 255 - (255 - base) * (255 - source) / 255
            np.testing.assert_allclose(pixel, (base + screened) / 2, atol=3)

            stage, far, near = [
                await call(session, "register_original", path=name)
                for name in ("stage.png", "far.png", "near.png")
            ]
            parallax = await call(
                session,
                "prepare_cutout_scene",
                id=stage["id"],
                duration=1,
                layers=[
                    {
                        "key": "Far",
                        "source": far["id"],
                        "offset": [95, 20],
                    },
                    {
                        "key": "Near",
                        "source": near["id"],
                        "offset": [95, 65],
                        "plane_depth": 100,
                    },
                ],
                camera={
                    "focal": 400,
                    "motion": [
                        {"time": 0, "ease": "linear"},
                        {"time": 1, "truck": [40, 0]},
                    ],
                },
            )
            for name, time in (("stage-0.png", 0), ("stage-1.png", 1)):
                await call(
                    session,
                    "render_cutout_scene",
                    id=parallax["id"],
                    output=name,
                    time=time,
                )

            centers = {}
            for name in ("stage-0.png", "stage-1.png"):
                pixels = np.asarray(Image.open(tmp_path / name).convert("RGB"))
                red = np.argwhere(
                    (pixels[..., 0] > 180)
                    & (pixels[..., 1] < 80)
                    & (pixels[..., 2] < 80)
                )
                green = np.argwhere(
                    (pixels[..., 1] > 180)
                    & (pixels[..., 0] < 80)
                    & (pixels[..., 2] < 80)
                )
                centers[name] = (red[:, 1].mean(), green[:, 1].mean())
            far_shift = centers["stage-1.png"][0] - centers["stage-0.png"][0]
            near_shift = centers["stage-1.png"][1] - centers["stage-0.png"][1]
            assert -41 < far_shift < -39
            assert near_shift < -50
            assert near_shift < far_shift - 10

    asyncio.run(run())
