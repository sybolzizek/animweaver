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


def test_group_compositing_revises_without_rebuilding_and_validates_scope(tmp_path):
    Image.new("RGB", (80, 60), (0, 0, 0)).save(tmp_path / "plate.png")
    Image.new("RGBA", (20, 20), (220, 20, 20, 255)).save(tmp_path / "red.png")
    Image.new("RGBA", (20, 20), (20, 220, 20, 255)).save(
        tmp_path / "green.png"
    )

    async def run():
        async with connect(tmp_path) as session:
            plate, red, green = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "red.png", "green.png")
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                groups=[
                    {
                        "key": "Composite",
                        "opacity": [{"time": 0, "value": 0.5}],
                        "blend_mode": "screen",
                        "plane_depth": 30,
                    }
                ],
                layers=[
                    {
                        "key": "Red",
                        "source": red["id"],
                        "offset": [20, 20],
                        "parent": "Composite",
                    },
                    {
                        "key": "Green",
                        "source": green["id"],
                        "offset": [30, 20],
                        "parent": "Composite",
                    },
                ],
                camera={"focal": 400},
            )
            group = scene["groups"][0]
            assert group["opacity"][0]["value"] == 0.5
            assert group["blend_mode"] == "screen" and group["plane_depth"] == 30

            source = tmp_path / red["files"]["image.png"]
            unavailable = source.with_suffix(".unavailable")
            source.rename(unavailable)
            try:
                revised = await call(
                    session,
                    "revise_cutout_scene",
                    id=scene["id"],
                    opacities={"Composite": [{"time": 0, "value": 0.75}]},
                    blend_modes={"Composite": "multiply"},
                    plane_depths={"Composite": 50},
                )
            finally:
                unavailable.rename(source)
            changed = revised["groups"][0]
            assert changed["opacity"][0]["value"] == 0.75
            assert changed["blend_mode"] == "multiply"
            assert changed["plane_depth"] == 50
            for name in ("cutout-Red.png", "cutout-Green.png"):
                assert (tmp_path / revised["files"][name]).read_bytes() == (
                    tmp_path / scene["files"][name]
                ).read_bytes()

            await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                groups=[
                    {
                        "key": "Broken",
                        "opacity": [{"time": 0, "value": 0.5}],
                    }
                ],
                layers=[
                    {"key": "A", "source": red["id"], "parent": "Broken"},
                    {"key": "Outside", "source": green["id"]},
                    {"key": "B", "source": red["id"], "parent": "Broken"},
                ],
                fail=True,
            )
            await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                groups=[
                    {
                        "key": "Masked",
                        "opacity": [{"time": 0, "value": 0.5}],
                    }
                ],
                layers=[
                    {"key": "Target", "source": red["id"], "parent": "Masked"},
                    {
                        "key": "Mask",
                        "source": green["id"],
                        "matte_only": True,
                    },
                ],
                mattes=[{"target": "Target", "mask": "Mask"}],
                fail=True,
            )

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_group_isolation_nested_opacity_and_group_parallax_render_in_browser(
    tmp_path,
):
    Image.new("RGB", (80, 60), (0, 0, 0)).save(tmp_path / "plate.png")
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(tmp_path / "red.png")
    Image.new("RGBA", (20, 20), (0, 255, 0, 255)).save(tmp_path / "green.png")
    Image.new("RGBA", (12, 12), (255, 255, 255, 255)).save(
        tmp_path / "white.png"
    )
    Image.new("RGB", (200, 100), (5, 5, 5)).save(tmp_path / "stage.png")
    Image.new("RGBA", (10, 10), (20, 20, 230, 255)).save(tmp_path / "blue.png")

    async def run():
        async with connect(tmp_path) as session:
            plate, red, green, white = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "red.png", "green.png", "white.png")
            ]
            isolated = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                groups=[
                    {
                        "key": "Composite",
                        "opacity": [{"time": 0, "value": 0.5}],
                    },
                    {
                        "key": "Outer",
                        "opacity": [{"time": 0, "value": 0.5}],
                    },
                    {
                        "key": "Inner",
                        "parent": "Outer",
                        "opacity": [{"time": 0, "value": 0.5}],
                    },
                ],
                layers=[
                    {
                        "key": "Red",
                        "source": red["id"],
                        "offset": [10, 20],
                        "parent": "Composite",
                    },
                    {
                        "key": "Green",
                        "source": green["id"],
                        "offset": [20, 20],
                        "parent": "Composite",
                    },
                    {
                        "key": "White",
                        "source": white["id"],
                        "offset": [55, 24],
                        "parent": "Inner",
                    },
                ],
            )
            await call(
                session,
                "render_cutout_scene",
                id=isolated["id"],
                output="groups.png",
                time=0,
            )
            with Image.open(tmp_path / "groups.png") as image:
                assert np.allclose(image.getpixel((14, 25))[:3], (128, 0, 0), atol=2)
                # The top green child fully replaces red before the group is faded.
                assert np.allclose(image.getpixel((24, 25))[:3], (0, 128, 0), atol=2)
                # Inner 0.5 opacity composed into outer 0.5 opacity.
                assert np.allclose(image.getpixel((58, 27))[:3], (64, 64, 64), atol=2)

            stage, blue = [
                await call(session, "register_original", path=name)
                for name in ("stage.png", "blue.png")
            ]
            parallax = await call(
                session,
                "prepare_cutout_scene",
                id=stage["id"],
                duration=1,
                groups=[{"key": "NearGroup", "plane_depth": 100}],
                layers=[
                    {
                        "key": "Far",
                        "source": blue["id"],
                        "offset": [95, 20],
                    },
                    {
                        "key": "NearA",
                        "source": red["id"],
                        "offset": [95, 55],
                        "parent": "NearGroup",
                    },
                    {
                        "key": "NearB",
                        "source": green["id"],
                        "offset": [105, 55],
                        "parent": "NearGroup",
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
            for name, time in (("group-stage-0.png", 0), ("group-stage-1.png", 1)):
                await call(
                    session,
                    "render_cutout_scene",
                    id=parallax["id"],
                    output=name,
                    time=time,
                )
            centers = {}
            for name in ("group-stage-0.png", "group-stage-1.png"):
                pixels = np.asarray(Image.open(tmp_path / name).convert("RGB"))
                blue_pixels = np.argwhere(
                    (pixels[..., 2] > 180) & (pixels[..., 0] < 80)
                )
                red_pixels = np.argwhere(
                    (pixels[..., 0] > 180) & (pixels[..., 1] < 80)
                )
                centers[name] = (blue_pixels[:, 1].mean(), red_pixels[:, 1].mean())
            far_shift = centers["group-stage-1.png"][0] - centers["group-stage-0.png"][0]
            group_shift = centers["group-stage-1.png"][1] - centers["group-stage-0.png"][1]
            assert -41 < far_shift < -39
            assert group_shift < -50
            assert group_shift < far_shift - 10

    asyncio.run(run())
