import asyncio
import os
import shutil
import wave

import cv2
import pytest
from PIL import Image

from test_stdio import call, connect


def write_silence(path, seconds=2, sample_rate=8000):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\0\0" * round(seconds * sample_rate))


async def make_scene(session, path, duration=1):
    source = await call(session, "register_original", path=path)
    return await call(
        session,
        "prepare_cutout_scene",
        id=source["id"],
        duration=duration,
        layers=[{"key": "Plate", "source": source["id"]}],
    )


def test_film_project_compiles_revises_and_round_trips(tmp_path):
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    source_workspace.mkdir()
    target_workspace.mkdir()
    Image.new("RGB", (64, 48), "#284d73").save(source_workspace / "blue.png")
    Image.new("RGB", (64, 48), "#b85f35").save(source_workspace / "orange.png")
    write_silence(source_workspace / "score.wav", seconds=1)

    async def build():
        async with connect(source_workspace) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {
                "register_audio",
                "create_film_project",
                "revise_film_project",
                "inspect_film_timeline",
                "render_film_project",
                "export_animweaver_project",
                "import_animweaver_project",
            } <= names
            blue = await make_scene(session, "blue.png")
            orange = await make_scene(session, "orange.png")
            audio = await call(session, "register_audio", path="score.wav")
            assert (audio["sample_rate"], audio["channels"], audio["duration"]) == (
                8000,
                1,
                1.0,
            )
            project = await call(
                session,
                "create_film_project",
                fps=8,
                shots=[
                    {"key": "Blue", "scene": blue["id"], "frames": 4},
                    {"key": "Orange", "scene": orange["id"], "frames": 4},
                ],
                audio=audio["id"],
                tempo_bpm=120,
                beats_per_bar=4,
                events=[
                    {
                        "shot": "Blue",
                        "frame": 2,
                        "kind": "step",
                        "duration_frames": 1,
                    },
                    {"shot": "Orange", "frame": 0, "kind": "cut"},
                ],
            )
            timeline = await call(
                session, "inspect_film_timeline", id=project["id"]
            )
            assert (timeline["frames"], timeline["duration"]) == (8, 1.0)
            assert [shot["start_frame"] for shot in timeline["shots"]] == [0, 4]
            assert [event["global_frame"] for event in timeline["events"]] == [2, 4]
            assert timeline["beat"]["frames_per_beat"] == 4
            assert all(
                boundary["offset_frames"] == 0
                for boundary in timeline["beat"]["boundaries"]
            )

            revised = await call(
                session,
                "revise_film_project",
                id=project["id"],
                shots=[
                    {"key": "Blue", "scene": blue["id"], "frames": 3},
                    {"key": "Orange", "scene": orange["id"], "frames": 5},
                ],
            )
            assert revised["parent"] == project["id"]
            assert revised["shots"][1]["start_frame"] == 3
            assert revised["warnings"][0]["code"] == "shot_boundaries_off_beat"

            await call(
                session,
                "create_film_project",
                fail=True,
                fps=8,
                shots=[{"key": "Blue", "scene": blue["id"], "frames": 9}],
            )
            await call(
                session,
                "create_film_project",
                fail=True,
                fps=8,
                shots=[{"key": "Blue", "scene": blue["id"], "frames": 4}],
                events=[{"shot": "Blue", "frame": 4, "kind": "late"}],
            )

            package = await call(
                session,
                "export_animweaver_project",
                id=project["id"],
                output="film.animweaver",
            )
            assert package["project"] == project["id"]
            return project["id"], audio["id"]

    project_id, audio_id = asyncio.run(build())
    shutil.copy2(
        source_workspace / "film.animweaver",
        target_workspace / "film.animweaver",
    )

    async def consume():
        async with connect(target_workspace) as session:
            imported = await call(
                session, "import_animweaver_project", path="film.animweaver"
            )
            project = imported["project"]
            assert project["id"] == project_id
            assert project["audio"] == audio_id
            assert imported["receipt"]["project"] == project_id
            assert imported["receipt"]["imported"] == imported["receipt"]["materials"]
            timeline = await call(
                session, "inspect_film_timeline", id=project["id"]
            )
            assert [shot["key"] for shot in timeline["shots"]] == ["Blue", "Orange"]

    asyncio.run(consume())


def test_film_project_renders_exact_shots_and_audio(tmp_path):
    if (
        not os.environ.get("REFERENCE_BROWSER_DEPS")
        or not os.environ.get("REFERENCE_BROWSER_EXECUTABLE")
        or not shutil.which("ffmpeg")
    ):
        pytest.skip("Set browser integration paths and FFmpeg")
    Image.new("RGB", (64, 48), "#184f84").save(tmp_path / "blue.png")
    Image.new("RGB", (64, 48), "#ca612b").save(tmp_path / "orange.png")
    write_silence(tmp_path / "score.wav", seconds=1)

    async def finish(session, job):
        async with asyncio.timeout(45):
            while True:
                state = await call(session, "inspect_film_video", id=job["id"])
                if state["status"] not in ("queued", "running"):
                    return state
                await asyncio.sleep(0.1)

    async def run():
        async with connect(tmp_path) as session:
            blue = await make_scene(session, "blue.png")
            orange = await make_scene(session, "orange.png")
            audio = await call(session, "register_audio", path="score.wav")
            project = await call(
                session,
                "create_film_project",
                fps=4,
                shots=[
                    {"key": "Blue", "scene": blue["id"], "frames": 2},
                    {"key": "Orange", "scene": orange["id"], "frames": 2},
                ],
                audio=audio["id"],
            )
            job = await call(
                session,
                "render_film_project",
                id=project["id"],
                output="film.mp4",
            )
            state = await finish(session, job)
            assert state["status"] == "completed", state
            result = state["render"]
            assert (result["frames"], result["fps"], result["duration"]) == (4, 4, 1)
            assert result["audio"] == audio["id"]
            assert [shot["key"] for shot in result["shots"]] == ["Blue", "Orange"]

        capture = cv2.VideoCapture(str(tmp_path / "film.mp4"))
        try:
            assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 4
            ok, first = capture.read()
            assert ok and first.shape[:2] == (48, 64)
            capture.set(cv2.CAP_PROP_POS_FRAMES, 2)
            ok, third = capture.read()
            assert ok
            assert first[..., 0].mean() > first[..., 2].mean()
            assert third[..., 2].mean() > third[..., 0].mean()
        finally:
            capture.release()

    asyncio.run(run())
