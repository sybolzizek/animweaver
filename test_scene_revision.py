import asyncio
import os

import numpy as np
from PIL import Image

from test_stdio import call, connect


def test_retime_refined_surface_without_rebinding_or_reassigning_pixels(tmp_path):
    fg = np.array([37, 69, 109])
    bg = np.array([237, 225, 201])
    y, x = np.indices((52, 102))
    a = np.clip(30.8 + y * 0.32 - x, 0, 1) * (x >= 10) * (8 <= y) * (y < 43)
    Image.fromarray(
        np.uint8(np.rint(a[..., None] * fg + (1 - a[..., None]) * bg))
    ).save(tmp_path / "still.png")
    Image.new("RGB", (102, 52), tuple(bg)).save(tmp_path / "plate.png")
    Image.fromarray(np.uint8(a >= 0.75) * 255).save(tmp_path / "hard.png")
    other = np.zeros(a.shape, dtype=np.uint8)
    other[20, 37] = 255  # Competing hard owner inside the estimated fringe.
    Image.fromarray(other).save(tmp_path / "other.png")

    async def run():
        async with connect(tmp_path) as session:
            still = await call(session, "register_original", path="still.png")
            plate = await call(session, "register_original", path="plate.png")
            split = await call(session, "split_image", id=still["id"])
            cut, neighbor = [
                await call(
                    session, "register_image_split_mask", id=split["id"], mask_path=path
                )
                for path in ("hard.png", "other.png")
            ]
            matte = await call(session, "refine_image_split_edges", id=cut["id"])
            assert matte["fractional_alpha_pixels"] > 0
            base = await call(
                session,
                "prepare_cutout_scene",
                id=still["id"],
                backing=plate["id"],
                duration=2,
                groups=[
                    {"key": "Rig", "pivot": [10, 8]},
                    {"key": "Unchanged", "motion": [{"time": 1, "rotate": 30}]},
                ],
                layers=[
                    {
                        "key": "Part",
                        "selection": cut["id"],
                        "mesh": {
                            "domain": [5, 4, 53, 48],
                            "bones": [{"key": "Rig", "selection": cut["id"]}],
                            "cell": 4,
                        },
                    },
                    {"key": "Neighbor", "selection": neighbor["id"]},
                ],
            )
            updated = await call(
                session,
                "revise_cutout_scene",
                id=base["id"],
                duration=2.5,
                edge_mattes={"Part": matte["id"]},
                motions={
                    "Rig": [{"time": 0}, {"time": 1, "move": [40, 0]}],
                    "Neighbor": [{"time": 2, "move": [0, 4]}],
                },
            )
            assert updated["parent"] == base["id"] and updated["duration"] == 2.5
            assert updated["groups"][1] == base["groups"][1]
            assert updated["backdrop_pixels"] == base["backdrop_pixels"]
            hard, soft = base["cutouts"][0], updated["cutouts"][0]
            for field in (
                "box",
                "mesh",
                "source_id",
                "pixels",
                "taken_by_other_cutouts",
            ):
                assert soft[field] == hard[field]
            assert soft["edge_matte_clipped_pixels"] >= 1
            for record in (base, updated):
                with Image.open(tmp_path / record["files"]["cutout-Part.png"]) as im:
                    assert im.getpixel((37 - 5, 20 - 4))[3] == 0
                    alpha = im.getpixel((37 - 5, 21 - 4))[3]
                    assert 60 < alpha < 200 if record is updated else alpha == 0
            with Image.open(tmp_path / updated["files"]["backing.png"]) as im:
                assert im.getpixel((37, 21)) == (*bg, 255)
            restored = await call(
                session,
                "revise_cutout_scene",
                id=updated["id"],
                edge_mattes={"Part": None},
                motions={"Rig": []},
            )
            assert (
                restored["groups"][0]["motion"] == []
                and restored["layers"][0]["edge_matte"] is None
            )
            assert updated["layers"][0]["edge_matte"] == matte["id"]
            await call(
                session,
                "revise_cutout_scene",
                id=base["id"],
                edge_mattes={"Neighbor": matte["id"]},
                fail=True,
            )
            await call(
                session,
                "revise_cutout_scene",
                id=base["id"],
                motions={"NoSuchNode": []},
                fail=True,
            )
            await call(
                session,
                "revise_cutout_scene",
                id=updated["id"],
                duration=0.9,
                fail=True,
            )
            if os.environ.get("REFERENCE_BROWSER_DEPS") and os.environ.get(
                "REFERENCE_BROWSER_EXECUTABLE"
            ):
                await call(
                    session,
                    "render_cutout_scene",
                    id=updated["id"],
                    time=1,
                    output="moved.png",
                )
                with Image.open(tmp_path / "moved.png") as frame:
                    assert frame.getpixel((37, 21)) == (
                        *bg,
                        255,
                    )  # No old outline left behind.
                    assert frame.getpixel((63, 21)) == (*fg, 255)
                    moved_edge = frame.getpixel((77, 21))[:3]
                    target = np.rint(a[21, 37] * fg + (1 - a[21, 37]) * bg)
                    np.testing.assert_allclose(moved_edge, target, atol=4)

    asyncio.run(run())


def test_timeline_revision_reuses_compiled_cutouts(tmp_path):
    Image.new("RGB", (60, 30), (7, 37, 62)).save(tmp_path / "plate.png")
    Image.new("RGBA", (10, 8), (201, 93, 42, 255)).save(tmp_path / "part.png")

    async def run():
        async with connect(tmp_path) as session:
            plate, part = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "part.png")
            ]
            base = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                groups=[{"key": "Rig", "pivot": [10, 10]}],
                layers=[
                    {
                        "key": "Part",
                        "source": part["id"],
                        "parent": "Rig",
                        "offset": [10, 10],
                    }
                ],
                duration=1,
            )
            # A timeline edit must not read the graph's source artwork a second
            # time. The previous portable bundle already holds the rasters.
            snapshot = tmp_path / part["files"]["image.png"]
            offline = snapshot.with_suffix(".unavailable")
            snapshot.rename(offline)
            try:
                revised = await call(
                    session,
                    "revise_cutout_scene",
                    id=base["id"],
                    duration=2,
                    motions={"Rig": [{"time": 0}, {"time": 1.5, "move": [28, 0]}]},
                )
            finally:
                offline.rename(snapshot)
            assert revised["parent"] == base["id"]
            for name in ("cutout-Part.png", "background.png"):
                assert (tmp_path / revised["files"][name]).read_bytes() == (
                    tmp_path / base["files"][name]
                ).read_bytes()
            assert revised["duration"] == 2 and base["groups"][0]["motion"] == []
            if os.environ.get("REFERENCE_BROWSER_DEPS") and os.environ.get(
                "REFERENCE_BROWSER_EXECUTABLE"
            ):
                await call(
                    session,
                    "render_cutout_scene",
                    id=revised["id"],
                    time=1.5,
                    output="after.png",
                )
                with Image.open(tmp_path / "after.png") as image:
                    assert image.getpixel((12, 12)) == (7, 37, 62, 255)
                    assert image.getpixel((40, 12)) == (201, 93, 42, 255)

    asyncio.run(run())
