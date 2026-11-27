import asyncio
import json
import os

import pytest
from PIL import Image

from test_stdio import call, connect


def _browser_ready():
    return os.environ.get("REFERENCE_BROWSER_DEPS") and os.environ.get(
        "REFERENCE_BROWSER_EXECUTABLE"
    )


def test_camera_and_action_timing_validate_as_timeline_data(tmp_path):
    Image.new("RGB", (100, 60), (21, 48, 73)).save(tmp_path / "plate.png")
    Image.new("RGBA", (10, 10), (224, 75, 45, 255)).save(tmp_path / "part.png")

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
                        "offset": [45, 20],
                        "motion": [
                            {"time": 0, "ease": "hold"},
                            {"time": 1, "move": [20, 0]},
                        ],
                    }
                ],
                camera={
                    "motion": [
                        {"time": 0, "ease": "linear"},
                        {"time": 1, "move": [5, 0], "zoom": 2},
                    ],
                    "shakes": [
                        {
                            "time": 0.4,
                            "duration": 0.2,
                            "amplitude": [2, 1],
                            "rotate": 0.4,
                            "seed": 17,
                        }
                    ],
                },
            )
            assert scene["camera"]["motion"][0]["ease"] == "linear"
            assert scene["layers"][0]["motion"][0]["ease"] == "hold"
            await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                camera={
                    "motion": [{"time": 0}, {"time": 1, "move": [5, 0]}],
                    "shakes": [],
                },
            )
            await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                camera={
                    "shakes": [
                        {"time": 0.9, "duration": 0.2, "amplitude": [1, 1]}
                    ]
                },
                fail=True,
            )

    asyncio.run(run())


@pytest.mark.skipif(not _browser_ready(), reason="Set browser integration paths")
def test_camera_render_hold_shake_and_coverage_are_deterministic(tmp_path):
    Image.new("RGB", (100, 60), (21, 48, 73)).save(tmp_path / "plate.png")
    Image.new("RGBA", (10, 10), (224, 75, 45, 255)).save(tmp_path / "part.png")

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
                        "offset": [45, 20],
                        "motion": [
                            {"time": 0, "ease": "hold"},
                            {"time": 1, "move": [20, 0]},
                        ],
                    }
                ],
                camera={
                    "motion": [
                        {"time": 0, "ease": "linear"},
                        {"time": 1, "move": [5, 0], "zoom": 2},
                    ],
                    "shakes": [
                        {
                            "time": 0.4,
                            "duration": 0.2,
                            "amplitude": [2, 1],
                            "rotate": 0.4,
                            "seed": 17,
                        }
                    ],
                },
            )
            await call(
                session,
                "render_cutout_scene",
                id=scene["id"],
                output="hold.png",
                time=0.5,
            )
            with Image.open(tmp_path / "hold.png") as image:
                # The layer is still at its first pose while the camera has moved and zoomed.
                red = image.getpixel((47, 25))
                assert red[0] > 180 and red[1] < 110
                assert image.getpixel((76, 25))[:3] == (21, 48, 73)

            for name in ("shake-a.png", "shake-b.png"):
                await call(
                    session,
                    "render_cutout_scene",
                    id=scene["id"],
                    output=name,
                    time=0.45,
                )
            assert (tmp_path / "shake-a.png").read_bytes() == (
                tmp_path / "shake-b.png"
            ).read_bytes()

            pan = await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                camera={
                    "motion": [
                        {"time": 0, "ease": "linear"},
                        {"time": 1, "move": [20, 0]},
                    ]
                },
            )
            audit = await call(
                session, "audit_cutout_scene", id=pan["id"], fps=4, max_frames=20
            )
            assert audit["max_unfilled"]["pixels"] > 0
            assert "unfilled_pixels_at_key_event" in audit["warnings"]
            report = json.loads(
                (tmp_path / audit["files"]["audit.json"]).read_text("utf-8")
            )
            assert any(
                "camera:motion" in event["reasons"] for event in report["events"]
            )

    asyncio.run(run())
