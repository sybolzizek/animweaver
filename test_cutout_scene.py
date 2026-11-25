import asyncio

import numpy as np
from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_revision_changes_pixel_ownership_but_not_draw_order(tmp_path):
    image = Image.new("RGB", (160, 96), "#0e2034")
    draw = ImageDraw.Draw(image)
    for left, color in [(40, "#f8dfa1"), (80, "#151a25"), (120, "#ffe9f0")]:
        draw.rectangle((left, 0, left + 39, 95), fill=color)
    image.save(tmp_path / "scene.png")

    async def run():
        async with connect(tmp_path) as session:
            original = await call(session, "register_original", path="scene.png")
            split = await call(session, "split_image", id=original["id"])

            async def select(*x):
                return await call(
                    session,
                    "select_image_split",
                    id=split["id"],
                    include=[[t, 48] for t in x],
                )

            red, gold, larger_red = (
                await select(58),
                await select(99, 139),
                await select(58, 139),
            )
            layers = [
                {"key": "Red", "selection": red["id"], "parent": "Rig"},
                {"key": "Gold", "selection": gold["id"]},
            ]
            groups = [
                {
                    "key": "Rig",
                    "pivot": [60, 40],
                    "motion": [
                        {"time": 0},
                        {"time": 1, "move": [4, 0], "scale": [1, 0.9]},
                    ],
                }
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=split["id"],
                layers=layers,
                groups=groups,
            )
            revised = await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                replacements={"Red": larger_red["id"]},
            )
            assert revised["parent"] == scene["id"]
            assert revised["claim_order"] == ["Gold", "Red"]
            assert [row["key"] for row in revised["cutouts"]] == ["Red", "Gold"]
            assert revised["cutouts"][1]["taken_by_other_cutouts"] > 0
            assert revised["groups"] == scene["groups"]
            assert (tmp_path / scene["files"]["scene.json"]).exists()
            assert (tmp_path / scene["files"]["cutout-scene.mjs"]).exists()
            assert not any(name.endswith(".html") for name in scene["files"])

            for version in (scene, revised):
                paths = version["files"]
                with Image.open(tmp_path / paths["background.png"]) as backdrop:
                    final = backdrop.copy()
                total = np.array(final.getchannel("A")) > 0
                for row in version["cutouts"]:
                    with Image.open(tmp_path / paths[row["file"]]) as image:
                        x, y, r, b = row["box"]
                        mask = np.array(image.getchannel("A")) > 0
                        assert not np.any(total[y:b, x:r] & mask)
                        total[y:b, x:r] |= mask
                        final.alpha_composite(image, (x, y))
                assert total.all()
                assert final.convert("RGB").tobytes() == image_source

            await call(
                session,
                "prepare_cutout_scene",
                id=split["id"],
                layers=layers,
                groups=[{"key": "Rig", "parent": "Rig"}],
                fail=True,
            )
            await call(
                session,
                "select_image_split",
                id=split["id"],
                include=[[58, 48]],
                exclude=[[58, 48]],
                fail=True,
            )

    image_source = image.tobytes()
    asyncio.run(run())
