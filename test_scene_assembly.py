import asyncio
import os

import pytest
from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_mount_underlap_and_appearance_in_same_render(tmp_path):
    if not (
        os.environ.get("REFERENCE_BROWSER_DEPS")
        and os.environ.get("REFERENCE_BROWSER_EXECUTABLE")
    ):
        pytest.skip("Set browser integration paths")
    background = (33, 48, 66, 255)
    Image.new("RGBA", (100, 80), background).save(tmp_path / "base.png")
    Image.new("RGBA", (32, 16), (110, 70, 45, 255)).save(tmp_path / "host.png")
    Image.new("RGBA", (8, 8), (230, 20, 170, 255)).save(tmp_path / "prop.png")
    Image.new("RGBA", (4, 8), (210, 170, 70, 255)).save(tmp_path / "front.png")
    Image.new("RGBA", (8, 4), (70, 82, 65, 255)).save(tmp_path / "underlap.png")
    Image.new("RGBA", (10, 10), (145, 96, 66, 255)).save(tmp_path / "chip.png")
    receiver = Image.new("RGBA", (28, 36), (200, 200, 200, 255))
    ImageDraw.Draw(receiver).rectangle((24, 0, 27, 35), fill=(0, 0, 0, 0))
    receiver.save(tmp_path / "receiver.png")

    async def run():
        async with connect(tmp_path) as session:
            guide = await session.read_resource("reference-materials://guide/compositing")
            assert "mount" in guide.contents[0].text and "underlap" in guide.contents[0].text
            ids = {
                name: (await call(session, "register_original", path=f"{name}.png"))[
                    "id"
                ]
                for name in (
                    "base",
                    "host",
                    "prop",
                    "front",
                    "underlap",
                    "chip",
                    "receiver",
                )
            }
            grade = await call(
                session,
                "match_cutout_appearance",
                source=ids["host"],
                source_box=[0, 0, 32, 16],
                reference=ids["chip"],
                reference_box=[0, 0, 10, 10],
                strength=1,
            )
            assert grade["source_pixels"] == 512
            split = await call(session, "split_image", id=ids["host"])
            tree = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[
                    {"key": "Host", "polygon": [[0, 0], [31, 0], [31, 15], [0, 15]]},
                ],
            )
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=ids["base"],
                duration=1,
                groups=[
                    {"key": "Fixed", "pivot": [18, 38]},
                    {
                        "key": "Moving",
                        "pivot": [45, 38],
                        "motion": [
                            {"time": 0},
                            {"time": 1, "move": [12, 6], "rotate": 90},
                        ],
                    },
                    {"key": "Mount", "pivot": [4, 4]},
                ],
                layers=[
                    {"key": "Receiver", "source": ids["receiver"], "offset": [52, 24]},
                    {
                        "key": "Host",
                        "selection": tree["children"][0]["selection"],
                        "offset": [16, 30],
                        "mesh": {
                            "bones": [
                                {"key": "Fixed", "tip": [35, 38]},
                                {"key": "Moving", "tip": [47, 38]},
                            ],
                            "blend": 2,
                            "cell": 2,
                        },
                    },
                    {"key": "Prop", "source": ids["prop"], "parent": "Mount"},
                    {
                        "key": "Fingers",
                        "source": ids["front"],
                        "parent": "Mount",
                        "offset": [4, 0],
                    },
                    {"key": "Chip", "source": ids["chip"], "offset": [6, 55]},
                ],
            )
            mount = {"layer": "Host", "point": [26, 8], "towards": [29, 8]}
            result = await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                mounts={"Mount": mount},
                underlaps={"Host": [{"source": ids["underlap"], "at": [-6, 6]}]},
                appearances={
                    "Host": {"grade": grade["grade"]},
                    "Chip": {
                        "light_axis": [1, 0],
                        "light": [{"time": 0}, {"time": 1, "gradient": 2}],
                    },
                },
                shadows=[
                    {
                        "caster": "Prop",
                        "receiver": "Receiver",
                        "offset": [18, 0],
                        "blur": 0,
                        "opacity": 0.5,
                    }
                ],
            )
            assert result["cutouts"][1]["underlap_pixels"] == 24
            assert result["cutouts"][1]["pixels"] == scene["cutouts"][1]["pixels"]
            assert result["cutouts"][1]["owned_source_box"] == [0, 0, 32, 16]
            assert "cutout-shadows.mjs" in result["files"]
            await call(
                session,
                "revise_cutout_scene",
                fail=True,
                id=result["id"],
                meshes={"Host": {"bones": [{"key": "Mount"}]}},
            )
            await call(
                session,
                "revise_cutout_scene",
                fail=True,
                id=result["id"],
                mounts={"Mount": mount | {"point": [26, 20]}},
            )
            reset = await call(
                session,
                "revise_cutout_scene",
                id=result["id"],
                mounts={"Mount": None},
                underlaps={"Host": []},
                appearances={"Host": None, "Chip": None},
                shadows=[],
            )
            assert reset["groups"][2]["mount"] is None and reset["shadows"] == []
            for t, output in [(0, "rest.png"), (1, "action.png")]:
                await call(
                    session,
                    "render_cutout_scene",
                    id=result["id"],
                    output=output,
                    time=t,
                )
            with Image.open(tmp_path / "rest.png") as frame:
                assert frame.getpixel((11, 37)) != background
                fitted = frame.getpixel((21, 32))
                assert all(
                    abs(actual - target) <= 4
                    for actual, target in zip(fitted, (145, 96, 66, 255))
                )
                assert frame.getpixel((39, 38))[:3] == (230, 20, 170)
                assert frame.getpixel((45, 38))[:3] == (210, 170, 70)
                assert frame.getpixel((7, 60)) == frame.getpixel((14, 60))
            with Image.open(tmp_path / "action.png") as frame:
                # Rotated mount follows the deformed surface, including orientation.
                assert frame.getpixel((57, 39))[:3] == (230, 20, 170)
                assert frame.getpixel((57, 44))[:3] == (210, 170, 70)
                assert 96 <= frame.getpixel((74, 41))[0] <= 103
                assert (
                    frame.getpixel((77, 41)) == background
                )  # shadow stops at the receiver cut
                assert frame.getpixel((53, 30))[:3] == (200, 200, 200)
                assert frame.getpixel((7, 60))[0] < 145 < frame.getpixel((14, 60))[0]
        with Image.open(tmp_path / "host.png") as source:
            assert source.getpixel((0, 0)) == (110, 70, 45, 255)

    asyncio.run(run())
