import asyncio
import json
import os
import shutil
import zipfile

import numpy as np
import pytest
from PIL import Image

from test_stdio import call, connect


def _browser_ready():
    return os.environ.get("REFERENCE_BROWSER_DEPS") and os.environ.get(
        "REFERENCE_BROWSER_EXECUTABLE"
    )


async def _build_parent(session):
    child_base, middle_base, parent_base, sprite = [
        await call(session, "register_original", path=name)
        for name in ("child.png", "middle.png", "parent.png", "sprite.png")
    ]
    child = await call(
        session,
        "prepare_cutout_scene",
        id=child_base["id"],
        duration=1,
        layers=[
            {
                "key": "Sprite",
                "source": sprite["id"],
                "offset": [2, 5],
                "motion": [
                    {"time": 0, "move": [0, 0], "ease": "linear"},
                    {"time": 1, "move": [10, 0]},
                ],
            }
        ],
    )
    middle = await call(
        session,
        "prepare_cutout_scene",
        id=middle_base["id"],
        duration=1,
        layers=[
            {
                "key": "ChildScene",
                "scene": child["id"],
                "offset": [1, 1],
            }
        ],
    )
    parent = await call(
        session,
        "prepare_cutout_scene",
        id=parent_base["id"],
        duration=1,
        layers=[
            {
                "key": "Early",
                "scene": middle["id"],
                "offset": [2, 3],
                "local_time": {"offset": 0, "scale": 0},
            },
            {
                "key": "Late",
                "scene": middle["id"],
                "offset": [30, 3],
                "local_time": {"offset": 1, "scale": 0},
            },
        ],
    )
    return child, middle, parent


def _write_inputs(workspace):
    Image.new("RGBA", (20, 16), (0, 0, 0, 0)).save(workspace / "child.png")
    Image.new("RGBA", (24, 18), (0, 0, 0, 0)).save(workspace / "middle.png")
    Image.new("RGB", (60, 28), (8, 12, 16)).save(workspace / "parent.png")
    Image.new("RGBA", (4, 4), (230, 45, 30, 255)).save(workspace / "sprite.png")


def test_scene_source_can_coexist_with_raster_seams(tmp_path):
    _write_inputs(tmp_path)
    Image.new("RGBA", (8, 8), (225, 70, 45, 255)).save(tmp_path / "seam-a.png")
    Image.new("RGBA", (8, 8), (45, 185, 225, 255)).save(tmp_path / "seam-b.png")

    async def run():
        async with connect(tmp_path) as session:
            child_base, parent_base, sprite, seam_a, seam_b = [
                await call(session, "register_original", path=name)
                for name in (
                    "child.png",
                    "parent.png",
                    "sprite.png",
                    "seam-a.png",
                    "seam-b.png",
                )
            ]
            child = await call(
                session,
                "prepare_cutout_scene",
                id=child_base["id"],
                layers=[{"key": "Sprite", "source": sprite["id"], "offset": [2, 4]}],
            )
            parent = await call(
                session,
                "prepare_cutout_scene",
                id=parent_base["id"],
                layers=[
                    {"key": "Nested", "scene": child["id"]},
                    {"key": "A", "source": seam_a["id"], "offset": [10, 8]},
                    {"key": "B", "source": seam_b["id"], "offset": [18, 8]},
                ],
                seams=[
                    {
                        "key": "Join",
                        "a": {"layer": "A", "rail": [[6, 1], [6, 6]]},
                        "b": {"layer": "B", "rail": [[1, 1], [1, 6]]},
                        "max_span": 20,
                    }
                ],
            )
            assert parent["cutouts"][0]["scene_id"] == child["id"]
            assert "file" not in parent["cutouts"][0]
            assert "seam-Join.png" in parent["files"]

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_three_scene_sources_are_independent_editable_instances(tmp_path):
    _write_inputs(tmp_path)

    async def run():
        async with connect(tmp_path) as session:
            child, middle, parent = await _build_parent(session)
            early, late = parent["cutouts"]
            assert early["scene_id"] == late["scene_id"] == middle["id"]
            assert middle["cutouts"][0]["scene_id"] == child["id"]
            assert "file" not in early and "file" not in late
            assert early["box"] == [2, 3, 26, 21]
            assert late["box"] == [30, 3, 54, 21]

            rendered = await call(
                session,
                "render_cutout_scene",
                id=parent["id"],
                output="instances.png",
                time=0.5,
                backend="three",
            )
            assert rendered["backend"] == "three"
            pixels = np.asarray(Image.open(tmp_path / "instances.png").convert("RGB"))

            red = (pixels[..., 0] > 180) & (pixels[..., 1] < 90)
            ys, xs = np.where(red)
            assert (xs.min(), xs.max(), ys.min(), ys.max()) == (5, 46, 9, 12)
            assert red[9:13, 5:9].all()
            assert red[9:13, 43:47].all()
            assert not red[9:13, 15:19].any()
            assert not red[9:13, 33:37].any()

            await call(
                session,
                "render_cutout_scene",
                id=parent["id"],
                output="canvas.png",
                time=0.5,
                backend="canvas2d",
                fail=True,
            )
            assert not (tmp_path / "canvas.png").exists()

    asyncio.run(run())


def test_scene_source_validates_kind_and_packages_dependency_closure(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_inputs(source)

    async def build():
        async with connect(source) as session:
            child, middle, parent = await _build_parent(session)
            sprite = await call(session, "register_original", path="sprite.png")
            base = await call(session, "register_original", path="parent.png")
            await call(
                session,
                "prepare_cutout_scene",
                id=base["id"],
                layers=[{"key": "Invalid", "scene": sprite["id"]}],
                fail=True,
            )
            revised = await call(
                session,
                "revise_cutout_scene",
                id=parent["id"],
                local_times={
                    "Early": {"offset": 0.25, "scale": 0},
                    "Late": {"offset": 0.75, "scale": 0},
                },
            )
            assert [item["scene_id"] for item in revised["cutouts"]] == [
                middle["id"],
                middle["id"],
            ]
            exported = await call(
                session,
                "export_animweaver_scene",
                id=revised["id"],
                output="nested.animweaver",
            )
            assert exported["materials"] >= 7
            return child["id"], middle["id"], revised["id"]

    child_id, middle_id, parent_id = asyncio.run(build())
    with zipfile.ZipFile(source / "nested.animweaver") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert child_id in manifest["materials"]
        assert middle_id in manifest["materials"]
        assert parent_id == manifest["root_scene"]

    shutil.copy2(source / "nested.animweaver", target / "nested.animweaver")

    async def consume():
        async with connect(target) as session:
            imported = await call(
                session,
                "import_animweaver_scene",
                path="nested.animweaver",
            )
            assert imported["scene"]["id"] == parent_id
            assert middle_id in {
                layer["scene"]
                for layer in imported["scene"]["layers"]
                if layer.get("scene")
            }

    asyncio.run(consume())
