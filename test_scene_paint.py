import asyncio
import os

import pytest
from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_paintover_and_local_mask_follow_their_own_rigs(tmp_path):
    if not (
        os.environ.get("REFERENCE_BROWSER_DEPS")
        and os.environ.get("REFERENCE_BROWSER_EXECUTABLE")
    ):
        pytest.skip("Set browser integration paths")
    backdrop = (60, 88, 118, 255)
    original = (192, 125, 90, 255)
    repair = (210, 160, 143, 255)
    Image.new("RGBA", (100, 72), backdrop).save(tmp_path / "back.png")
    leg = Image.new("RGBA", (24, 24), original)
    ImageDraw.Draw(leg).rectangle((0, 10, 23, 11), fill=(30, 20, 25, 255))
    leg.save(tmp_path / "leg.png")
    Image.new("RGBA", (24, 5), repair).save(tmp_path / "repair.png")
    Image.new("RGBA", (9, 9), (255, 255, 255, 255)).save(tmp_path / "stencil.png")
    for name, box in (("left", (0, 0, 13, 23)), ("right", (10, 0, 23, 23))):
        domain_piece = Image.new("RGBA", (24, 24))
        ImageDraw.Draw(domain_piece).rectangle(box, fill=original)
        domain_piece.save(tmp_path / f"{name}.png")

    async def run():
        async with connect(tmp_path) as session:
            ids = {
                name: (await call(session, "register_original", path=f"{name}.png"))[
                    "id"
                ]
                for name in ("back", "leg", "repair", "stencil", "left", "right")
            }
            base = await call(
                session,
                "prepare_cutout_scene",
                id=ids["back"],
                duration=1.5,
                groups=[
                    {
                        "key": "LegBone",
                        "motion": [{"time": 0}, {"time": 1, "move": [18, 0]}],
                    }
                ],
                layers=[
                    {
                        "key": "Leg",
                        "source": ids["leg"],
                        "offset": [10, 20],
                        "mesh": {"bones": [{"key": "LegBone"}], "cell": 4},
                    },
                    {
                        "key": "Window",
                        "source": ids["stencil"],
                        "offset": [44, 24],
                        "matte_only": True,
                        "motion": [{"time": 0}, {"time": 1, "move": [-9, 2]}],
                    },
                ],
            )
            edited = await call(
                session,
                "revise_cutout_scene",
                id=base["id"],
                paintovers={"Leg": [{"source": ids["repair"], "at": [0, 9]}]},
                mattes=[
                    {
                        "target": "Leg",
                        "mask": "Window",
                        "operation": "erase",
                        "windows": [[0.8, 1.25]],
                    }
                ],
            )
            assert base["cutouts"][0]["pixels"] == edited["cutouts"][0]["pixels"]
            assert edited["cutouts"][0]["owned_source_box"] == [0, 0, 24, 24]
            for time, path in (
                (0, "start.png"),
                (1, "masked.png"),
                (1.5, "outside.png"),
            ):
                await call(
                    session,
                    "render_cutout_scene",
                    id=edited["id"],
                    time=time,
                    output=path,
                )
            with Image.open(tmp_path / "start.png") as im:
                assert im.getpixel((17, 30)) == repair
                assert (
                    im.getpixel((45, 27)) == backdrop
                )  # matte-only never paints white
            with Image.open(tmp_path / "masked.png") as im:
                assert im.getpixel((36, 30)) == backdrop
                assert im.getpixel((46, 30)) == repair
                assert im.getpixel((46, 39)) == original
            with Image.open(tmp_path / "outside.png") as im:
                assert im.getpixel((36, 30)) == repair
            assert Image.open(tmp_path / "leg.png").getpixel((3, 10)) == (
                30,
                20,
                25,
                255,
            )
            # Distinct opaque bounds but a shared lattice across the cloth join.
            grid = {
                "domain": [0, 0, 24, 24],
                "cell": 5,
                "bones": [
                    {"key": "Fixed", "tip": [10, 44]},
                    {"key": "Moving", "tip": [34, 44]},
                ],
                "blend": 15,
            }
            joined = await call(
                session,
                "prepare_cutout_scene",
                id=ids["back"],
                duration=1.5,
                groups=[
                    {"key": "Fixed", "pivot": [10, 20]},
                    {
                        "key": "Moving",
                        "pivot": [34, 20],
                        "motion": [
                            {"time": 0},
                            {"time": 1, "move": [4, 3], "rotate": 16},
                        ],
                    },
                ],
                layers=[
                    {
                        "key": part.title(),
                        "source": ids[part],
                        "offset": [10, 20],
                        "mesh": grid,
                    }
                    for part in ("left", "right")
                ],
            )
            a, b = joined["cutouts"]
            assert a["owned_source_box"] != b["owned_source_box"]
            assert a["source_box"] == b["source_box"] and a["mesh"] == b["mesh"]
            await call(
                session,
                "render_cutout_scene",
                id=joined["id"],
                time=1,
                output="shared.png",
            )
            with Image.open(tmp_path / "shared.png") as im:
                assert im.getpixel((24, 32)) == original
                assert im.getpixel((5, 32)) == backdrop

    asyncio.run(run())
