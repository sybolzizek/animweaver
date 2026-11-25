import asyncio
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_corrected_mask_import_preserves_source_pixels_and_crop_coordinates(tmp_path):
    source = Image.new("RGB", (70, 50), "#534a3e")
    source.save(tmp_path / "source.png")
    imported = Image.new("RGBA", (12, 10), (255, 0, 0, 0))
    ImageDraw.Draw(imported).rectangle((2, 1, 8, 7), fill=(255, 0, 0, 255))
    imported.putpixel((5, 5), (255, 0, 0, 100))
    imported.save(tmp_path / "edited.png")

    async def run():
        async with connect(tmp_path) as session:
            original = await call(session, "register_original", path="source.png")
            split = await call(session, "split_image", id=original["id"])
            request = dict(id=split["id"], mask_path="edited.png", box=[20, 15, 32, 25])
            mask = await call(session, "register_image_split_mask", **request)
            assert mask["source_box"] == [22, 16, 29, 23]
            assert mask["pixels"] == 48
            assert mask["method"] == "imported_mask"
            with Image.open(tmp_path / mask["files"]["image.png"]) as result:
                assert result.getpixel((0, 0)) == (83, 74, 62, 255)
                assert result.getpixel((3, 4)) == (0, 0, 0, 0)
            tree = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[{"key": "Corrected", "selection": mask["id"]}],
            )
            assert tree["children"][0]["pixels"] == 48
            # A stored hard mask can itself be reimported at its cropped coordinates.
            copied = await call(
                session,
                "register_image_split_mask",
                id=mask["id"],
                mask_path=mask["files"]["mask.png"],
                box=mask["source_box"],
            )
            assert copied["mask_sha256"] == mask["mask_sha256"]
            assert copied["inputs"]["previous_selection"] == mask["id"]
            for overrides in (
                {"box": None},
                {"box": [60, 45, 72, 55]},
                {"threshold": 0},
                {"mask_path": str(Path(tmp_path).parent / "outside.png")},
            ):
                await call(
                    session,
                    "register_image_split_mask",
                    fail=True,
                    **(request | overrides),
                )

    asyncio.run(run())


def test_recursive_cutouts_have_single_ownership_and_immutable_branches(tmp_path):
    image = Image.new("RGBA", (180, 120), (55, 98, 38, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 140, 100), fill=(76, 116, 166, 200))
    draw.rectangle((104, 64, 155, 97), fill=(39, 55, 69, 0))
    image.save(tmp_path / "source.png")

    def rectangle(key, x, y, r, b):
        return {"key": key, "polygon": [[x, y], [r, y], [r, b], [x, b]]}

    async def run():
        async with connect(tmp_path) as session:
            source = await call(session, "register_original", path="source.png")
            split = await call(session, "split_image", id=source["id"])
            start = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[rectangle("Actor", 20, 20, 155, 100)],
            )
            actor, rest = start["children"]
            assert actor["path"] == "root/Actor"
            assert rest["path"] == "root/@rest"
            child = await call(
                session,
                "split_image_region",
                id=start["id"],
                parent=actor["path"],
                cuts=[
                    rectangle("Upper", 0, 0, 110, 68),
                    rectangle("Lower", 88, 50, 179, 89),
                ],
            )
            assert child["children"][0]["discarded_outside_parent"] > 0
            assert child["children"][0]["lost_to_siblings"] > 0
            assert sum(x["pixels"] for x in child["children"]) == actor["pixels"]
            tree = await call(session, "inspect_material", id=child["id"])
            original_tree = await call(session, "inspect_material", id=start["id"])
            assert original_tree["nodes"][actor["path"]]["children"] == []
            assert tree["nodes"]["root/@rest"] == original_tree["nodes"]["root/@rest"]

            basis = tree["nodes"]["root/Actor/Upper"]["selection"]
            nested = await call(
                session,
                "split_image_region",
                id=child["id"],
                parent="root/Actor/Upper",
                cuts=[rectangle("Tip", 32, 28, 61, 42)],
            )
            full = await call(session, "inspect_material", id=nested["id"])
            sub_tree = await call(
                session,
                "split_image_region",
                id=basis,
                cuts=[rectangle("Tip", 32, 28, 61, 42)],
            )
            assert sub_tree["parent_box"] == tree["nodes"]["root/Actor/Upper"]["box"]
            owner = np.zeros((120, 180), bool)
            restored = np.zeros((120, 180, 4), np.uint8)
            for row in full["nodes"].values():
                if row["children"]:
                    continue
                record = await call(session, "inspect_material", id=row["selection"])
                x, y, r, b = record["source_box"]
                with Image.open(tmp_path / record["files"]["mask.png"]) as file:
                    mask = np.asarray(file) > 0
                with Image.open(tmp_path / record["files"]["image.png"]) as file:
                    pixels = np.asarray(file)
                assert not (owner[y:b, x:r] & mask).any()
                owner[y:b, x:r][mask] = True
                restored[y:b, x:r][mask] = pixels[mask]
            assert owner.all()
            np.testing.assert_array_equal(restored, np.asarray(image))

            replacement = await call(
                session,
                "split_image_region",
                id=nested["id"],
                parent="root/Actor",
                replace=True,
                cuts=[{"key": "Other", "selection": basis}],
            )
            resolved = await call(session, "inspect_material", id=replacement["id"])
            assert "root/Actor/Upper/Tip" not in resolved["nodes"]
            assert "root/Actor/Other" in resolved["nodes"]
            assert "root/Actor/Upper/Tip" in full["nodes"]
            await call(
                session,
                "split_image_region",
                id=child["id"],
                parent="root/Actor",
                cuts=[rectangle("Other", 22, 24, 55, 87)],
                fail=True,
            )
            await call(
                session,
                "split_image_region",
                id=child["id"],
                parent="root/Actor/@rest",
                cuts=[rectangle("Missing", 1, 1, 2, 2)],
                fail=True,
            )

            parts = [x["selection"] for x in nested["children"]]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=split["id"],
                layers=[{"key": "Pivot", "selection": parts[0]}],
            )
            assert scene["layers"][0]["selection"] == parts[0]

    asyncio.run(run())


def test_export_preview_and_local_replacement_keep_unrelated_branches(tmp_path):
    # Use exact source colors that differ from a binary artifact.
    source = Image.new("RGB", (72, 56), "#534a3e")
    source.save(tmp_path / "source.png")
    patch = Image.new("L", (40, 13))
    ImageDraw.Draw(patch).rectangle((4, 2, 36, 10), fill=255)
    patch.save(tmp_path / "patch.png")

    def rect(key, x, y, r, b):
        return {"key": key, "polygon": [[x, y], [r, y], [r, b], [x, b]]}

    async def run():
        async with connect(tmp_path) as session:
            image = await call(session, "register_original", path="source.png")
            split = await call(session, "split_image", id=image["id"])
            tree = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[rect("Figure", 5, 6, 65, 48)],
            )
            tree = await call(
                session,
                "split_image_region",
                id=tree["id"],
                parent="root/Figure",
                cuts=[rect("Sleeve", 10, 12, 31, 35), rect("Bag", 34, 8, 43, 39)],
            )
            tree = await call(
                session,
                "split_image_region",
                id=tree["id"],
                parent="root/Figure/Sleeve",
                cuts=[rect("Fixed", 12, 13, 24, 17), rect("Clipped", 11, 20, 22, 23)],
            )
            before = await call(session, "inspect_material", id=tree["id"])
            branch = "root/Figure/Sleeve"
            packet = await call(
                session,
                "export_cutout_region",
                id=tree["id"],
                branch=branch,
                box=[0, 20, 40, 33],
            )
            assert packet["source_box"] == [0, 20, 40, 33]
            assert packet["split"] == split["id"]
            with Image.open(tmp_path / packet["files"]["image.png"]) as pixels:
                assert pixels.size == (40, 13) and pixels.getpixel((0, 0)) == (
                    83,
                    74,
                    62,
                    255,
                )
            with Image.open(tmp_path / packet["files"]["mask.png"]) as mask:
                assert mask.getpixel((15, 2)) == 255 and mask.getpixel((35, 2)) == 0
            edited = await call(
                session,
                "register_image_split_mask",
                id=packet["split"],
                mask_path="patch.png",
                box=packet["source_box"],
            )
            request = dict(id=tree["id"], branch=branch, selection=edited["id"])
            preview = await call(session, "replace_cutout_region", **request)
            assert preview["added"] == 63 and preview["removed"] == 88
            assert (
                preview["blocked_by_siblings"] == 27
                and preview["ignored_outside_parent"] == 9
            )
            assert (
                "tree" not in preview
                and (tmp_path / preview["files"]["preview.png"]).is_file()
            )
            result = await call(
                session, "replace_cutout_region", preview_only=False, **request
            )
            assert result["id"] == preview["id"]
            after = await call(session, "inspect_material", id=result["tree"])
            for path in ["root/@rest", "root/Figure/Bag", f"{branch}/Fixed"]:
                assert before["nodes"][path] == after["nodes"][path]
            assert after["nodes"][f"{branch}/Clipped"]["pixels"] == 24
            assert (
                sum(
                    after["nodes"][c]["pixels"]
                    for c in after["nodes"]["root/Figure"]["children"]
                )
                == before["nodes"]["root/Figure"]["pixels"]
            )
            assert (
                sum(after["nodes"][c]["pixels"] for c in result["node"]["children"])
                == result["node"]["pixels"]
            )
            assert (
                result["node"]["pixels"] == before["nodes"][branch]["pixels"] + 63 - 88
            )
            assert (await call(session, "inspect_material", id=before["id"])) == before
            erased = await call(
                session,
                "replace_cutout_region",
                id=after["id"],
                branch=branch,
                box=[11, 22, 23, 24],
                preview_only=False,
            )
            assert erased["removed"] == 24 and erased["dropped_branches"] == [
                f"{branch}/Clipped"
            ]

    asyncio.run(run())
