import asyncio
import json
import shutil
import zipfile

from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_animweaver_package_round_trips_editable_scene_and_motion(tmp_path):
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    source_workspace.mkdir()
    target_workspace.mkdir()
    Image.new("RGB", (96, 72), "#28425b").save(source_workspace / "background.png")
    sprite = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    ImageDraw.Draw(sprite).rectangle((2, 2, 17, 17), fill="#e79039")
    sprite.save(source_workspace / "sprite.png")
    (source_workspace / "motion.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "width": 96,
                "height": 72,
                "duration": 1,
                "tracks": {
                    "RootPoint": [
                        {"time": 0, "x": 10, "y": 10},
                        {"time": 1, "x": 30, "y": 18},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    async def build():
        async with connect(source_workspace) as session:
            base = await call(session, "register_original", path="background.png")
            layer = await call(session, "register_original", path="sprite.png")
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=base["id"],
                duration=1,
                groups=[{"key": "Root", "pivot": [10, 10]}],
                layers=[
                    {
                        "key": "Sprite",
                        "source": layer["id"],
                        "parent": "Root",
                        "offset": [10, 10],
                    }
                ],
            )
            motion = await call(
                session, "register_motion_reference", path="motion.json"
            )
            revised = await call(
                session,
                "retarget_motion_reference",
                scene_id=scene["id"],
                motion_id=motion["id"],
                bindings=[{"node": "Root", "position": "RootPoint"}],
                fps=4,
            )
            exported = await call(
                session,
                "export_animweaver_scene",
                id=revised["id"],
                output="scene.animweaver",
            )
            assert exported["scene"] == revised["id"]
            assert exported["materials"] >= 4
            return revised["id"]

    source_scene = asyncio.run(build())
    shutil.copy2(
        source_workspace / "scene.animweaver",
        target_workspace / "scene.animweaver",
    )

    async def consume():
        async with connect(target_workspace) as session:
            imported = await call(
                session, "import_animweaver_scene", path="scene.animweaver"
            )
            scene = imported["scene"]
            assert scene["id"] == source_scene
            assert imported["receipt"]["imported"] == imported["receipt"]["materials"]
            assert scene["motion_retarget"]["reference"] in scene["inputs"].values()
            root = next(group for group in scene["groups"] if group["key"] == "Root")
            assert root["motion"][-1]["move"] == [20.0, 8.0]

            edited = await call(
                session,
                "revise_cutout_scene",
                id=scene["id"],
                motions={"Root": [{"time": 0}, {"time": 1, "move": [4, 2]}]},
            )
            assert edited["groups"][0]["motion"][-1]["move"] == [4.0, 2.0]
            repacked = await call(
                session,
                "export_animweaver_scene",
                id=edited["id"],
                output="edited.animweaver",
            )
            assert repacked["materials"] > imported["receipt"]["materials"]

            again = await call(
                session, "import_animweaver_scene", path="scene.animweaver"
            )
            assert again["receipt"]["imported"] == 0
            assert again["receipt"]["reused"] == again["receipt"]["materials"]

    asyncio.run(consume())


def test_animweaver_package_rejects_archive_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    Image.new("RGB", (32, 32), "#334455").save(workspace / "image.png")

    async def build():
        async with connect(workspace) as session:
            image = await call(session, "register_original", path="image.png")
            scene = await call(
                session,
                "prepare_cutout_scene",
                id=image["id"],
                layers=[{"key": "Image", "source": image["id"]}],
            )
            await call(
                session,
                "export_animweaver_scene",
                id=scene["id"],
                output="unsafe.animweaver",
            )

    asyncio.run(build())
    with zipfile.ZipFile(workspace / "unsafe.animweaver", "a") as archive:
        archive.writestr("../outside.txt", "not allowed")

    async def reject():
        async with connect(workspace) as session:
            await call(
                session,
                "import_animweaver_scene",
                path="unsafe.animweaver",
                fail=True,
            )
        assert not (tmp_path / "outside.txt").exists()

    asyncio.run(reject())
