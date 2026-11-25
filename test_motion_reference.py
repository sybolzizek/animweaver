import asyncio
import json

from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_motion_reference_retargets_only_mapped_scene_nodes(tmp_path):
    Image.new("RGB", (100, 100), "#203040").save(tmp_path / "background.png")
    sprite = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
    ImageDraw.Draw(sprite).ellipse((2, 2, 21, 21), fill=(240, 120, 40, 255))
    sprite.save(tmp_path / "sprite.png")
    motion = {
        "schema": 1,
        "width": 100,
        "height": 100,
        "duration": 1,
        "tracks": {
            "RootPoint": [
                {"time": 0, "x": 10, "y": 10},
                {"time": 0.5, "x": 15, "y": 12, "confidence": 0.1},
                {"time": 1, "x": 30, "y": 20},
            ],
            "Shoulder": [
                {"time": 0, "x": 20, "y": 20},
                {"time": 1, "x": 20, "y": 20},
            ],
            "Hand": [
                {"time": 0, "x": 40, "y": 20},
                {"time": 1, "x": 20, "y": 40},
            ],
        },
    }
    (tmp_path / "motion.json").write_text(
        json.dumps(motion), encoding="utf-8"
    )

    async def run():
        async with connect(tmp_path) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {
                "register_motion_reference",
                "register_pose_reference",
                "inspect_motion_reference",
                "retarget_motion_reference",
                "audit_cutout_scene",
                "export_animweaver_scene",
                "import_animweaver_scene",
            } <= names
            resources = await session.list_resources()
            assert "reference-materials://schema/motion-reference" in {
                str(resource.uri) for resource in resources.resources
            }
            schema = await session.read_resource(
                "reference-materials://schema/motion-reference"
            )
            assert json.loads(schema.contents[0].text)["properties"]["schema"]
            background = await call(
                session, "register_original", path="background.png"
            )
            source = await call(session, "register_original", path="sprite.png")
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=background["id"],
                duration=1,
                groups=[
                    {"key": "Root", "pivot": [10, 10]},
                    {"key": "Arm", "parent": "Root", "pivot": [20, 20]},
                    {"key": "Unmapped", "pivot": [50, 50]},
                ],
                layers=[
                    {
                        "key": "Sprite",
                        "source": source["id"],
                        "parent": "Arm",
                        "offset": [8, 8],
                    }
                ],
            )
            reference = await call(
                session, "register_motion_reference", path="motion.json"
            )
            assert reference["tracks"] == ["Hand", "RootPoint", "Shoulder"]
            inspected = await call(
                session,
                "inspect_motion_reference",
                id=reference["id"],
                tracks=["RootPoint"],
                sample_limit=2,
            )
            assert inspected["tracks"]["RootPoint"]["sample_count"] == 3
            assert len(inspected["tracks"]["RootPoint"]["samples"]) == 2

            revised = await call(
                session,
                "retarget_motion_reference",
                scene_id=scene["id"],
                motion_id=reference["id"],
                bindings=[
                    {"node": "Root", "position": "RootPoint"},
                    {
                        "node": "Arm",
                        "direction": ["Shoulder", "Hand"],
                        "scale_axis": "x",
                    },
                ],
                target_start=0.5,
                target_duration=1,
                fps=2,
                minimum_confidence=0.25,
                extend_scene=True,
            )
            assert revised["duration"] == 1.5
            groups = {group["key"]: group for group in revised["groups"]}
            assert groups["Root"]["motion"][-1]["move"] == [20.0, 10.0]
            assert groups["Arm"]["motion"][-1]["rotate"] == 90.0
            assert groups["Arm"]["motion"][-1]["scale"] == [1.0, 1.0]
            assert groups["Unmapped"]["motion"] == []
            assert revised["motion_retarget"]["reference"] == reference["id"]
            assert revised["inputs"]["motion_reference"] == reference["id"]
            reports = {
                row["node"]: row for row in revised["motion_retarget"]["reports"]
            }
            assert reports["Root"]["repaired_low_confidence_samples"] == 1
            assert scene["groups"][0]["motion"] == []

            await call(
                session,
                "retarget_motion_reference",
                fail=True,
                scene_id=scene["id"],
                motion_id=reference["id"],
                bindings=[{"node": "Root", "position": "Missing"}],
            )

    asyncio.run(run())


def test_motion_reference_rejects_non_increasing_samples(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "width": 10,
                "height": 10,
                "duration": 1,
                "tracks": {
                    "Point": [
                        {"time": 0.5, "x": 1, "y": 1},
                        {"time": 0.5, "x": 2, "y": 2},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    async def run():
        async with connect(tmp_path) as session:
            await call(
                session, "register_motion_reference", path="bad.json", fail=True
            )

    asyncio.run(run())
