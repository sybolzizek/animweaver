import asyncio
import json
import os

import pytest
from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_scene_audit_reports_unfilled_key_event(tmp_path):
    if not os.environ.get("REFERENCE_BROWSER_DEPS") or not os.environ.get(
        "REFERENCE_BROWSER_EXECUTABLE"
    ):
        pytest.skip("Set browser integration paths")
    source = Image.new("RGB", (80, 60), "#24445f")
    ImageDraw.Draw(source).rectangle((10, 10, 29, 29), fill="#e56a34")
    source.save(tmp_path / "source.png")

    async def run():
        async with connect(tmp_path) as session:
            original = await call(session, "register_original", path="source.png")
            split = await call(session, "split_image", id=original["id"])
            tree = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[
                    {
                        "key": "Tile",
                        "polygon": [[10, 10], [30, 10], [30, 30], [10, 30]],
                    }
                ],
            )
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=original["id"],
                duration=1,
                layers=[
                    {
                        "key": "Tile",
                        "selection": tree["children"][0]["selection"],
                        "motion": [{"time": 0}, {"time": 1, "move": [25, 0]}],
                    }
                ],
            )
            audit = await call(
                session,
                "audit_cutout_scene",
                id=scene["id"],
                fps=4,
                max_frames=20,
            )
            assert audit["parent"] == scene["id"]
            assert audit["frames_sampled"] == 5
            assert audit["max_unfilled"]["pixels"] > 0
            assert "unfilled_pixels_at_key_event" in audit["warnings"]
            report = json.loads(
                (tmp_path / audit["files"]["audit.json"]).read_text("utf-8")
            )
            assert report["scene"] == scene["id"]
            assert {event["time"] for event in report["events"]} >= {0, 1}

    asyncio.run(run())
