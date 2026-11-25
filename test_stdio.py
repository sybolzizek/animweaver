import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image, ImageDraw


server = Path(__file__).with_name("server.py")


@asynccontextmanager
async def connect(workspace):
    settings = StdioServerParameters(
        command=sys.executable,
        args=[str(server), "--workspace", str(workspace)],
        cwd=str(workspace),
        env={
            key: os.environ[key]
            for key in ("REFERENCE_BROWSER_DEPS", "REFERENCE_BROWSER_EXECUTABLE")
            if key in os.environ
        },
    )
    async with (
        stdio_client(settings) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def call(session, name, *, fail=False, **arguments):
    result = await session.call_tool(name, arguments)
    assert bool(result.isError) == fail, result
    if result.structuredContent is not None:
        return result.structuredContent
    if not fail and len(result.content) == 1 and result.content[0].type == "text":
        return json.loads(result.content[0].text)
    return result.content


def test_reference_materials_from_independent_client(tmp_path):
    workspace = tmp_path / "inputs"
    workspace.mkdir()
    Image.new("RGB", (120, 90), "#264f72").save(workspace / "source.png")
    Image.new("RGB", (100, 80), "#ad8751").save(workspace / "supplement.png")
    Image.new("RGB", (16, 16)).save(tmp_path / "outside.png")

    async def run():
        async with connect(workspace) as session:
            tools = await session.list_tools()
            names = {x.name for x in tools.tools}
            assert {
                "register_original",
                "extract_region",
                "render_cutout_scene",
            } <= names
            assert not names.intersection(
                {
                    "segment_candidates",
                    "track_patch",
                    "video_frame",
                    "export_shot_edit",
                    "prepare_motion_shot",
                    "fit_procedural_scene",
                    "open_structure_study",
                }
            )
            original = await call(session, "register_original", path="source.png")
            assert original["origin"] == "original" and original["width"] == 120
            assert (await call(session, "register_original", path="source.png"))[
                "id"
            ] == original["id"]
            region = await call(
                session,
                "extract_region",
                id=original["id"],
                box=[10, 10, 81, 72],
                polygon=[[20, 20], [70, 30], [20, 60]],
            )
            with Image.open(workspace / region["files"]["image.png"]) as image:
                assert image.size == (71, 62) and image.getpixel((0, 0))[3] == 0
                assert image.getpixel((15, 20))[3] == 255
            preview = await call(
                session, "preview_material", id=region["id"], file="mask.png"
            )
            assert (
                len(preview) == 1
                and preview[0].type == "image"
                and preview[0].mimeType == "image/png"
            )
            measure = await call(
                session, "measure_path", id=region["id"], points=[[10, 10], [40, 50]]
            )
            assert measure["total_px"] == 50
            request = await call(
                session,
                "request_supplement",
                id=region["id"],
                instruction="补一张无高光纹样参考",
                expected_media="image",
            )
            supplement = await call(
                session,
                "register_supplement",
                request_id=request["id"],
                path="supplement.png",
                provider="test fixture, no provider invocation",
            )
            await call(session, "register_original", path="../outside.png", fail=True)

        async with connect(workspace) as session:
            evidence = await call(session, "inspect_material", id=supplement["id"])
            assert [
                evidence["origin"],
                *[x["origin"] for x in evidence["lineage"]],
            ] == ["generated", "request", "derived", "original"]
            assert [x["id"] for x in evidence["lineage"]] == [
                request["id"],
                region["id"],
                original["id"],
            ]

    asyncio.run(run())


def test_whole_image_split_keeps_source_rgba(tmp_path):
    # Frame-spanning edges make multiple deterministic fallback parts without
    # assuming that a pixel-only algorithm can recognize closed objects.
    source = Image.new("RGBA", (200, 120), (15, 30, 64, 255))
    draw = ImageDraw.Draw(source)
    draw.rectangle((71, 0, 139, 119), fill=(250, 236, 177, 220))
    draw.rectangle((140, 0, 199, 119), fill=(53, 123, 11, 0))
    source.save(tmp_path / "scene.png")

    async def run():
        async with connect(tmp_path) as session:
            original = await call(session, "register_original", path="scene.png")
            split = await call(session, "split_image", id=original["id"])
            assert split["inference"]["backend"] == "edges-only"
            row = await call(session, "inspect_material", id=split["id"])
            assert (
                sum(part["pixels"] for part in row["parts"])
                == source.width * source.height
            )
            assert len(row["parts"]) > 1
            ids = [part["id"] for part in row["parts"]]
            selection = await call(
                session, "select_image_split", id=split["id"], regions=ids[:2]
            )
            assert selection["pixels"] == sum(
                part["pixels"] for part in row["parts"][:2]
            )
            rightmost = max(row["parts"], key=lambda part: part["box"][0])
            edge_selection = await call(
                session, "select_image_split", id=split["id"], regions=[rightmost["id"]]
            )
            matte = await call(
                session, "refine_image_split_edges", id=edge_selection["id"]
            )
            assert matte["parent"] == edge_selection["id"]
            with Image.open(tmp_path / matte["files"]["image.png"]) as crop:
                with Image.open(tmp_path / matte["files"]["alpha.png"]) as alpha:
                    assert crop.getchannel("A").tobytes() == alpha.tobytes()
            x, _, _, _ = matte["source_box"]
            assert x > 0
            await call(session, "refine_image_split_edges", id=split["id"], fail=True)
            await call(
                session,
                "select_image_split",
                id=split["id"],
                regions=["P999"],
                fail=True,
            )
            await call(
                session,
                "select_image_split",
                id=split["id"],
                include=[[-1, 0]],
                fail=True,
            )
            reassembled = await call(session, "recompose_image_split", id=split["id"])
            assert reassembled["changed_pixels"] == 0
            with Image.open(tmp_path / reassembled["files"]["image.png"]) as output:
                assert output.tobytes() == source.tobytes()
            # Recomposition really reads the cutouts; it cannot silently return the reference image.
            part = row["parts"][0]
            path = tmp_path / row["files"][part["image"]]
            with Image.open(path) as cutout:
                altered = cutout.copy()
            x, y = part["point"]
            altered.putpixel(
                (x - part["box"][0], y - part["box"][1]), (240, 7, 98, 255)
            )
            altered.save(path)
            changed = await call(session, "recompose_image_split", id=split["id"])
            assert changed["changed_pixels"] == 1

    asyncio.run(run())


def test_compare_render_keeps_non_anchor_geometry_visible(tmp_path):
    Image.new("RGB", (100, 90), "#c87154").save(tmp_path / "reference.png")
    Image.new("RGB", (140, 120), "#805641").save(tmp_path / "render.png")

    async def run():
        async with connect(tmp_path) as session:
            reference = await call(session, "register_original", path="reference.png")
            candidate = await call(
                session,
                "register_render",
                path="render.png",
                capture_note="renderer-independent fixture",
            )
            crop = await call(
                session, "extract_region", id=candidate["id"], box=[5, 5, 105, 95]
            )
            assert (
                crop["parent"] == candidate["id"] and candidate["origin"] == "rendered"
            )
            anchors = [
                {"name": "A", "reference": [20, 20], "candidate": [20, 20]},
                {"name": "B", "reference": [50, 20], "candidate": [80, 20]},
            ]
            checks = [
                {"name": "unfitted", "reference": [40, 30], "candidate": [60, 54]}
            ]
            result = await call(
                session,
                "compare_images",
                reference_id=reference["id"],
                candidate_id=candidate["id"],
                alignment="similarity",
                anchors=anchors,
                checks=checks,
                focus_box=[16, 16, 76, 66],
                reference_polygon=[[14, 14], [75, 14], [75, 65], [14, 65]],
                candidate_polygon=[[8, 8], [108, 8], [108, 102], [8, 102]],
            )
            assert result["candidate_to_reference"] == [[0.5, 0, 10], [0, 0.5, 10]]
            assert result["metrics"]["fit_rms_px"] == 0
            assert result["checks"][0]["offset_px"] == [0, 7]
            assert 0 < result["metrics"]["mask_iou"] < 1
            assert set(result["files"]) == {
                "reference.png",
                "registered.png",
                "overlay.png",
                "difference.png",
                "contours.png",
                "landmarks.png",
            }
            with Image.open(tmp_path / result["files"]["difference.png"]) as diff:
                assert diff.size == (60, 50) and diff.getpixel((59, 49))[3] == 0
            evidence = await call(session, "inspect_material", id=result["id"])
            assert evidence["input_lineage"]["candidate"][0]["origin"] == "rendered"
            preview = await call(
                session, "preview_material", id=result["id"], file="contours.png"
            )
            assert preview[0].type == "image"
            await call(
                session,
                "compare_images",
                reference_id=reference["id"],
                candidate_id=candidate["id"],
                alignment="similarity",
                anchors=anchors,
                checks=anchors,
                fail=True,
            )

    asyncio.run(run())
