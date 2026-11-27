import asyncio
import os
import shutil

import numpy as np
import pytest
from PIL import Image, ImageDraw

from test_stdio import call, connect


def _browser_ready():
    return os.environ.get("REFERENCE_BROWSER_DEPS") and os.environ.get(
        "REFERENCE_BROWSER_EXECUTABLE"
    )


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_three_backend_matches_canvas_planes_local_time_opacity_and_order(tmp_path):
    Image.new("RGB", (80, 48), (12, 24, 36)).save(tmp_path / "plate.png")
    red_image = Image.new("RGBA", (12, 10), (220, 40, 20, 255))
    ImageDraw.Draw(red_image).rectangle((0, 5, 11, 9), fill=(35, 65, 220, 255))
    red_image.save(tmp_path / "red.png")
    Image.new("RGBA", (12, 10), (20, 210, 60, 255)).save(tmp_path / "green.png")

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
                    }
                ],
                layers=[
                    {
                        "key": "Red",
                        "source": red["id"],
                        "parent": "Cycle",
                        "offset": [8, 10],
                        "opacity": [
                            {"time": 0, "value": 1, "ease": "linear"},
                            {"time": 2, "value": 0.5},
                        ],
                    },
                    {
                        "key": "Green",
                        "source": green["id"],
                        "offset": [18, 10],
                        "depth": [
                            {"time": 0, "z": 2},
                            {"time": 0.5, "z": -1},
                        ],
                    },
                ],
            )
            for label, time in (("early", 0.25), ("late", 0.75), ("repeat", 1.25)):
                for backend in ("canvas2d", "three"):
                    result = await call(
                        session,
                        "render_cutout_scene",
                        id=scene["id"],
                        output=f"{label}-{backend}.png",
                        time=time,
                        backend=backend,
                    )
                    assert result["backend"] == backend

                canvas = np.asarray(
                    Image.open(tmp_path / f"{label}-canvas2d.png").convert("RGBA")
                ).astype(int)
                three = np.asarray(
                    Image.open(tmp_path / f"{label}-three.png").convert("RGBA")
                ).astype(int)
                difference = np.abs(canvas - three)
                assert difference.mean() < 1.5
                assert np.quantile(difference, 0.99) <= 8

            unsupported = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                layers=[
                    {
                        "key": "Red",
                        "source": red["id"],
                        "blend_mode": "overlay",
                    }
                ],
            )
            await call(
                session,
                "render_cutout_scene",
                id=unsupported["id"],
                output="unsupported.png",
                time=0,
                backend="three",
                fail=True,
            )
            assert not (tmp_path / "unsupported.png").exists()

            if shutil.which("ffmpeg"):
                job = await call(
                    session,
                    "render_cutout_scene",
                    id=scene["id"],
                    output="three.mp4",
                    fps=4,
                    backend="three",
                )
                async with asyncio.timeout(30):
                    while True:
                        state = await call(session, "inspect_cutout_video", id=job["id"])
                        if state["status"] not in ("queued", "running"):
                            break
                        await asyncio.sleep(0.1)
                assert state["status"] == "completed", state
                assert state["render"]["backend"] == "three"
                assert (tmp_path / "three.mp4").exists()

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_three_render_targets_match_nested_isolated_group_opacity(tmp_path):
    Image.new("RGB", (80, 60), (0, 0, 0)).save(tmp_path / "plate.png")
    Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(tmp_path / "red.png")
    Image.new("RGBA", (20, 20), (0, 255, 0, 255)).save(tmp_path / "green.png")
    Image.new("RGBA", (12, 12), (255, 255, 255, 255)).save(tmp_path / "white.png")

    async def run():
        async with connect(tmp_path) as session:
            plate, red, green, white = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "red.png", "green.png", "white.png")
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                groups=[
                    {"key": "Composite", "opacity": [{"time": 0, "value": 0.5}]},
                    {"key": "Outer", "opacity": [{"time": 0, "value": 0.5}]},
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
            for backend in ("canvas2d", "three"):
                await call(
                    session,
                    "render_cutout_scene",
                    id=scene["id"],
                    output=f"groups-{backend}.png",
                    time=0,
                    backend=backend,
                )
            canvas = np.asarray(Image.open(tmp_path / "groups-canvas2d.png").convert("RGB")).astype(int)
            three = np.asarray(Image.open(tmp_path / "groups-three.png").convert("RGB")).astype(int)
            difference = np.abs(canvas - three)
            assert difference.mean() < 1
            assert np.quantile(difference, 0.99) <= 3
            np.testing.assert_allclose(three[25, 14], (128, 0, 0), atol=2)
            np.testing.assert_allclose(three[25, 24], (0, 128, 0), atol=2)
            np.testing.assert_allclose(three[27, 58], (64, 64, 64), atol=2)

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_three_camera_depth_and_isolated_group_projection_match_canvas(tmp_path):
    Image.new("RGB", (200, 120), (9, 18, 29)).save(tmp_path / "stage.png")
    Image.new("RGBA", (14, 12), (230, 25, 20, 255)).save(tmp_path / "far.png")
    Image.new("RGBA", (14, 12), (20, 225, 45, 255)).save(tmp_path / "near.png")
    Image.new("RGBA", (12, 16), (25, 80, 230, 255)).save(tmp_path / "group.png")

    async def run():
        async with connect(tmp_path) as session:
            stage, far, near, group = [
                await call(session, "register_original", path=name)
                for name in ("stage.png", "far.png", "near.png", "group.png")
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=stage["id"],
                duration=1,
                groups=[
                    {
                        "key": "DepthGroup",
                        "plane_depth": 35,
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [-8, 5], "rotate": -4},
                        ],
                    }
                ],
                layers=[
                    {"key": "Far", "source": far["id"], "offset": [70, 20]},
                    {
                        "key": "Near",
                        "source": near["id"],
                        "offset": [94, 72],
                        "plane_depth": 80,
                    },
                    {
                        "key": "Grouped",
                        "source": group["id"],
                        "offset": [145, 48],
                        "parent": "DepthGroup",
                    },
                ],
                camera={
                    "pivot": [88, 54],
                    "focal": 320,
                    "motion": [
                        {"time": 0, "ease": "linear"},
                        {
                            "time": 1,
                            "move": [9, -5],
                            "truck": [28, 7],
                            "dolly": 24,
                            "zoom": 1.12,
                            "rotate": 6,
                        },
                    ],
                    "shakes": [
                        {
                            "time": 0.35,
                            "duration": 0.35,
                            "amplitude": [2.5, 1.5],
                            "rotate": 0.6,
                            "frequency": 14,
                            "seed": 23,
                        }
                    ],
                },
            )
            for label, time in (("start", 0), ("middle", 0.5), ("end", 1)):
                for backend in ("canvas2d", "three"):
                    await call(
                        session,
                        "render_cutout_scene",
                        id=scene["id"],
                        output=f"camera-{label}-{backend}.png",
                        time=time,
                        backend=backend,
                    )
                canvas = np.asarray(
                    Image.open(tmp_path / f"camera-{label}-canvas2d.png").convert("RGBA")
                ).astype(int)
                three = np.asarray(
                    Image.open(tmp_path / f"camera-{label}-three.png").convert("RGBA")
                ).astype(int)
                difference = np.abs(canvas - three)
                assert difference.mean() < 2.5
                assert np.quantile(difference, 0.99) <= 24

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_gpu_compositor_matches_canvas_layer_and_group_blends(tmp_path):
    Image.new("RGB", (128, 52), (60, 100, 140)).save(tmp_path / "plate.png")
    Image.new("RGBA", (18, 18), (120, 35, 20, 255)).save(tmp_path / "red.png")
    Image.new("RGBA", (18, 18), (40, 205, 70, 255)).save(tmp_path / "green.png")
    Image.new("RGBA", (18, 18), (25, 70, 220, 255)).save(tmp_path / "blue.png")

    async def run():
        async with connect(tmp_path) as session:
            plate, red, green, blue = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "red.png", "green.png", "blue.png")
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                groups=[
                    {
                        "key": "ScreenGroup",
                        "blend_mode": "screen",
                        "opacity": [{"time": 0, "value": 0.65}],
                    }
                ],
                layers=[
                    {
                        "key": "Add",
                        "source": red["id"],
                        "offset": [8, 17],
                        "blend_mode": "add",
                        "opacity": [{"time": 0, "value": 0.55}],
                    },
                    {
                        "key": "Multiply",
                        "source": green["id"],
                        "offset": [36, 17],
                        "blend_mode": "multiply",
                        "opacity": [{"time": 0, "value": 0.6}],
                    },
                    {
                        "key": "Screen",
                        "source": blue["id"],
                        "offset": [64, 17],
                        "blend_mode": "screen",
                        "opacity": [{"time": 0, "value": 0.7}],
                    },
                    {
                        "key": "GroupRed",
                        "source": red["id"],
                        "offset": [92, 17],
                        "parent": "ScreenGroup",
                    },
                    {
                        "key": "GroupBlue",
                        "source": blue["id"],
                        "offset": [98, 17],
                        "parent": "ScreenGroup",
                    },
                ],
            )
            for backend in ("canvas2d", "three"):
                await call(
                    session,
                    "render_cutout_scene",
                    id=scene["id"],
                    output=f"blends-{backend}.png",
                    time=0,
                    backend=backend,
                )
            canvas = np.asarray(Image.open(tmp_path / "blends-canvas2d.png").convert("RGB")).astype(int)
            three = np.asarray(Image.open(tmp_path / "blends-three.png").convert("RGB")).astype(int)
            difference = np.abs(canvas - three)
            assert difference.mean() < 1.5
            assert np.quantile(difference, 0.99) <= 8
            for x in (14, 42, 70, 102):
                np.testing.assert_allclose(three[25, x], canvas[25, x], atol=2)
                assert tuple(three[25, x]) != (60, 100, 140)

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_gpu_matte_keep_erase_windows_and_isolated_scope_match_canvas(tmp_path):
    Image.new("RGB", (100, 60), (25, 35, 45)).save(tmp_path / "plate.png")
    Image.new("RGBA", (40, 20), (205, 35, 20, 255)).save(tmp_path / "target.png")
    Image.new("RGBA", (24, 20), (255, 255, 255, 255)).save(tmp_path / "keep.png")
    Image.new("RGBA", (8, 20), (255, 255, 255, 255)).save(tmp_path / "erase.png")

    async def run():
        async with connect(tmp_path) as session:
            plate, target, keep, erase = [
                await call(session, "register_original", path=name)
                for name in ("plate.png", "target.png", "keep.png", "erase.png")
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=plate["id"],
                duration=1,
                groups=[
                    {
                        "key": "MaskedGroup",
                        "opacity": [{"time": 0, "value": 0.8}],
                        "plane_depth": 20,
                    }
                ],
                layers=[
                    {
                        "key": "Target",
                        "source": target["id"],
                        "offset": [12, 20],
                        "parent": "MaskedGroup",
                        "blend_mode": "screen",
                    },
                    {
                        "key": "KeepMask",
                        "source": keep["id"],
                        "offset": [20, 20],
                        "parent": "MaskedGroup",
                        "matte_only": True,
                        "opacity": [{"time": 0, "value": 0.75}],
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [8, 0]},
                        ],
                    },
                    {
                        "key": "EraseMask",
                        "source": erase["id"],
                        "offset": [30, 20],
                        "parent": "MaskedGroup",
                        "matte_only": True,
                        "opacity": [{"time": 0, "value": 0.5}],
                    },
                ],
                mattes=[
                    {
                        "target": "Target",
                        "mask": "KeepMask",
                        "operation": "keep",
                        "windows": [[0.25, 0.75]],
                    },
                    {
                        "target": "Target",
                        "mask": "EraseMask",
                        "operation": "erase",
                        "windows": [[0.25, 0.75]],
                    },
                ],
                camera={
                    "focal": 240,
                    "motion": [
                        {
                            "time": 0,
                            "truck": [8, 0],
                            "dolly": 12,
                            "zoom": 1.05,
                        }
                    ],
                },
            )
            red_counts = {}
            for label, time in (("before", 0.1), ("active", 0.5), ("after", 0.9)):
                for backend in ("canvas2d", "three"):
                    await call(
                        session,
                        "render_cutout_scene",
                        id=scene["id"],
                        output=f"matte-{label}-{backend}.png",
                        time=time,
                        backend=backend,
                    )
                canvas = np.asarray(
                    Image.open(tmp_path / f"matte-{label}-canvas2d.png").convert("RGB")
                ).astype(int)
                three = np.asarray(
                    Image.open(tmp_path / f"matte-{label}-three.png").convert("RGB")
                ).astype(int)
                difference = np.abs(canvas - three)
                assert difference.mean() < 1.5
                assert np.quantile(difference, 0.99) <= 10
                red_counts[label] = np.count_nonzero(
                    (three[..., 0] > 120) & (three[..., 0] > three[..., 1] + 35)
                )
            assert red_counts["active"] < red_counts["before"] * 0.7
            assert abs(red_counts["before"] - red_counts["after"]) <= 2

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_gpu_buffer_geometry_skinning_supports_many_bones_and_matte(tmp_path):
    Image.new("RGB", (140, 90), (32, 42, 54)).save(tmp_path / "stage.png")
    cloth = Image.new("RGBA", (70, 30), (0, 0, 0, 0))
    draw = ImageDraw.Draw(cloth)
    draw.rectangle((0, 0, 69, 9), fill=(225, 45, 35, 255))
    draw.rectangle((0, 10, 69, 19), fill=(35, 210, 80, 255))
    draw.rectangle((0, 20, 69, 29), fill=(35, 75, 225, 255))
    draw.rectangle((8, 0, 13, 29), fill=(235, 200, 45, 255))
    cloth.save(tmp_path / "cloth.png")
    Image.new("RGBA", (10, 10), (255, 255, 255, 255)).save(tmp_path / "hole.png")
    Image.new("RGBA", (12, 4), (225, 30, 180, 255)).save(tmp_path / "prop.png")

    async def run():
        async with connect(tmp_path) as session:
            stage, cloth_id, hole, prop = [
                await call(session, "register_original", path=name)
                for name in ("stage.png", "cloth.png", "hole.png", "prop.png")
            ]
            pivots = [(30, 45), (47, 45), (64, 45), (81, 45), (99, 45)]
            changes = [(0, 0, 0), (0, -5, -8), (2, 8, 15), (-2, -6, -12), (3, 4, 10)]
            groups = []
            for index, ((x, y), (dx, dy, rotate)) in enumerate(zip(pivots, changes)):
                groups.append(
                    {
                        "key": f"Bone{index}",
                        "pivot": [x, y],
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [dx, dy], "rotate": rotate},
                        ],
                    }
                )
            groups.append(
                {
                    "key": "Mount",
                    "pivot": [0, 2],
                    "mount": {
                        "layer": "Cloth",
                        "point": [34, 15],
                        "towards": [44, 15],
                    },
                }
            )
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=stage["id"],
                duration=1.2,
                groups=groups,
                layers=[
                    {
                        "key": "Cloth",
                        "source": cloth_id["id"],
                        "offset": [30, 30],
                        "plane_depth": 40,
                        "blend_mode": "multiply",
                        "opacity": [{"time": 0, "value": 0.88}],
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [2, 1]},
                        ],
                        "mesh": {
                            "bones": [
                                {"key": f"Bone{index}", "tip": [x + 10, y]}
                                for index, (x, y) in enumerate(pivots)
                            ],
                            "blend": 32,
                            "cell": 5,
                        },
                    },
                    {
                        "key": "Hole",
                        "source": hole["id"],
                        "offset": [62, 40],
                        "plane_depth": 40,
                        "matte_only": True,
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [5, -2]},
                        ],
                    },
                    {
                        "key": "Prop",
                        "source": prop["id"],
                        "parent": "Mount",
                    },
                ],
                mattes=[
                    {
                        "target": "Cloth",
                        "mask": "Hole",
                        "operation": "erase",
                        "windows": [[0.75, 1.1]],
                    }
                ],
                camera={
                    "focal": 300,
                    "motion": [
                        {"time": 0, "ease": "linear"},
                        {
                            "time": 1,
                            "move": [3, -2],
                            "truck": [12, 3],
                            "dolly": 15,
                            "zoom": 1.04,
                            "rotate": 2,
                        },
                    ],
                },
            )
            mesh = scene["cutouts"][0]["mesh"]
            assert len(mesh["bones"]) == 5
            assert len(mesh["weights"][0]) == 5
            for label, time in (("rest", 0), ("deformed", 1)):
                for backend in ("canvas2d", "three"):
                    await call(
                        session,
                        "render_cutout_scene",
                        id=scene["id"],
                        output=f"mesh-{label}-{backend}.png",
                        time=time,
                        backend=backend,
                    )
                canvas = np.asarray(
                    Image.open(tmp_path / f"mesh-{label}-canvas2d.png").convert("RGBA")
                ).astype(int)
                three = np.asarray(
                    Image.open(tmp_path / f"mesh-{label}-three.png").convert("RGBA")
                ).astype(int)
                difference = np.abs(canvas - three)
                assert difference.mean() < 3
                assert np.quantile(difference, 0.99) <= 32

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_gpu_surface_queries_drive_mesh_seams_and_reports(tmp_path):
    background = (30, 60, 100)
    cloth = (190, 70, 42)
    cover = (26, 175, 70)
    for key, size, fill in (
        ("back", (110, 72), background),
        ("top", (20, 18), cloth),
        ("bottom", (20, 18), cloth),
        ("cover", (3, 19), cover),
    ):
        Image.new("RGB", size, fill).save(tmp_path / f"{key}.png")

    async def run():
        async with connect(tmp_path) as session:
            ids = {
                key: (await call(session, "register_original", path=f"{key}.png"))["id"]
                for key in ("back", "top", "bottom", "cover")
            }
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=ids["back"],
                duration=2,
                groups=[
                    {
                        "key": "Shift",
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [8, 10], "ease": "linear"},
                            {"time": 2, "move": [10, 30]},
                        ],
                    }
                ],
                layers=[
                    {
                        "key": "Top",
                        "source": ids["top"],
                        "offset": [30, 12],
                        "plane_depth": 20,
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "rotate": 12, "move": [3, -5]},
                        ],
                        "pivot": [40, 30],
                    },
                    {
                        "key": "Bottom",
                        "source": ids["bottom"],
                        "offset": [30, 30],
                        "plane_depth": 20,
                        "mesh": {"bones": [{"key": "Shift"}], "cell": 5},
                    },
                    {
                        "key": "Occluder",
                        "source": ids["cover"],
                        "offset": [44, 24],
                        "plane_depth": 20,
                    },
                ],
                camera={
                    "focal": 260,
                    "motion": [
                        {
                            "time": 0,
                            "move": [2, -1],
                            "truck": [6, 0],
                            "dolly": 8,
                            "zoom": 1.03,
                        }
                    ],
                },
            )
            seam = {
                "key": "Joint",
                "a": {"layer": "Top", "rail": [[2, 16], [17, 16]]},
                "b": {"layer": "Bottom", "rail": [[2, 2], [17, 2]]},
                "max_span": 28,
            }
            joined = await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                seams=[seam],
            )
            for label, time, expected in (
                ("joined", 1, "drawn"),
                ("limit", 2, "too_wide"),
            ):
                reports = {}
                for backend in ("canvas2d", "three"):
                    result = await call(
                        session,
                        "render_cutout_scene",
                        id=joined["id"],
                        output=f"seam-{label}-{backend}.png",
                        time=time,
                        backend=backend,
                    )
                    reports[backend] = result["seam_report"][0]
                    assert reports[backend]["status"] == expected
                if expected == "drawn":
                    assert abs(reports["canvas2d"]["span"] - reports["three"]["span"]) < 1e-4
                canvas = np.asarray(
                    Image.open(tmp_path / f"seam-{label}-canvas2d.png").convert("RGB")
                ).astype(int)
                three = np.asarray(
                    Image.open(tmp_path / f"seam-{label}-three.png").convert("RGB")
                ).astype(int)
                difference = np.abs(canvas - three)
                assert difference.mean() < 3
                assert np.quantile(difference, 0.99) <= 28

            crossed = await call(
                session,
                "revise_cutout_scene",
                id=joined["id"],
                seams=[
                    seam
                    | {
                        "max_span": 45,
                        "b": {"layer": "Bottom", "rail": [[17, 2], [2, 2]]},
                    }
                ],
            )
            crossing = await call(
                session,
                "render_cutout_scene",
                id=crossed["id"],
                output="seam-crossed-three.png",
                time=1,
                backend="three",
            )
            assert crossing["seam_report"][0]["crossings"] > 0

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_gpu_local_light_and_projected_soft_shadows_match_canvas(tmp_path):
    Image.new("RGB", (132, 84), (82, 104, 126)).save(tmp_path / "stage.png")
    lit = Image.new("RGBA", (24, 18), (0, 0, 0, 0))
    lit_draw = ImageDraw.Draw(lit)
    lit_draw.rectangle((0, 0, 23, 8), fill=(210, 72, 38, 255))
    lit_draw.rectangle((0, 9, 23, 17), fill=(36, 112, 215, 210))
    lit.save(tmp_path / "lit.png")
    mesh = Image.new("RGBA", (32, 20), (0, 0, 0, 0))
    mesh_draw = ImageDraw.Draw(mesh)
    mesh_draw.rectangle((0, 0, 31, 19), fill=(54, 188, 92, 235))
    mesh_draw.rectangle((0, 0, 8, 19), fill=(228, 196, 52, 255))
    mesh.save(tmp_path / "mesh.png")
    receiver = Image.new("RGBA", (54, 18), (188, 178, 156, 220))
    ImageDraw.Draw(receiver).rectangle((3, 3, 50, 14), fill=(224, 216, 194, 235))
    receiver.save(tmp_path / "receiver.png")
    caster = Image.new("RGBA", (14, 14), (0, 0, 0, 0))
    ImageDraw.Draw(caster).ellipse((1, 1, 12, 12), fill=(238, 82, 62, 230))
    caster.save(tmp_path / "caster.png")

    async def run():
        async with connect(tmp_path) as session:
            stage, lit_id, mesh_id, receiver_id, caster_id = [
                await call(session, "register_original", path=name)
                for name in (
                    "stage.png",
                    "lit.png",
                    "mesh.png",
                    "receiver.png",
                    "caster.png",
                )
            ]
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=stage["id"],
                duration=1,
                groups=[
                    {
                        "key": "Bend",
                        "pivot": [82, 26],
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [5, 3], "rotate": 7},
                        ],
                    }
                ],
                layers=[
                    {
                        "key": "LitPlane",
                        "source": lit_id["id"],
                        "offset": [12, 10],
                        "appearance": {
                            "light_axis": [1, 0.25],
                            "light": [
                                {"time": 0, "exposure": -0.15, "gradient": 0.4},
                                {"time": 1, "exposure": 0.35, "gradient": 1.2},
                            ],
                        },
                    },
                    {
                        "key": "LitMesh",
                        "source": mesh_id["id"],
                        "offset": [66, 12],
                        "mesh": {"bones": [{"key": "Bend"}], "cell": 5},
                        "appearance": {
                            "light_axis": [-0.5, 1],
                            "light": [
                                {"time": 0, "exposure": 0.1, "gradient": -0.5},
                                {"time": 1, "exposure": 0.45, "gradient": 0.75},
                            ],
                        },
                    },
                    {
                        "key": "Receiver",
                        "source": receiver_id["id"],
                        "offset": [38, 56],
                    },
                    {
                        "key": "Caster",
                        "source": caster_id["id"],
                        "offset": [48, 42],
                        "opacity": [{"time": 0, "value": 0.8}],
                        "motion": [
                            {"time": 0, "ease": "linear"},
                            {"time": 1, "move": [8, -3], "rotate": 18},
                        ],
                    },
                ],
                shadows=[
                    {
                        "caster": "Caster",
                        "receiver": "Receiver",
                        "offset": [13, 12],
                        "scale": [1.25, 0.7],
                        "blur": 2,
                        "opacity": 0.62,
                        "color": [38, 30, 24],
                    },
                    {
                        "caster": "Caster",
                        "offset": [38, -10],
                        "scale": [1.1, 1.1],
                        "blur": 4,
                        "opacity": 0.45,
                        "color": [22, 34, 52],
                    },
                ],
            )
            frames = {}
            for label, time in (("start", 0), ("end", 1)):
                for backend in ("canvas2d", "three"):
                    await call(
                        session,
                        "render_cutout_scene",
                        id=scene["id"],
                        output=f"light-shadow-{label}-{backend}.png",
                        time=time,
                        backend=backend,
                    )
                canvas = np.asarray(
                    Image.open(tmp_path / f"light-shadow-{label}-canvas2d.png").convert("RGBA")
                ).astype(int)
                three = np.asarray(
                    Image.open(tmp_path / f"light-shadow-{label}-three.png").convert("RGBA")
                ).astype(int)
                difference = np.abs(canvas - three)
                assert difference.mean() < 4
                assert np.quantile(difference, 0.99) <= 40
                frames[label] = three
            assert np.abs(frames["end"] - frames["start"]).mean() > 2

    asyncio.run(run())
