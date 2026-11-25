import asyncio
import json

import pytest

from test_stdio import call, connect


def test_mediapipe_frames_become_pixel_motion_tracks(tmp_path):
    frames = []
    for frame_index in range(2):
        frames.append(
            {
                "timestamp_ms": frame_index * 40,
                "pose_landmarks": [
                    {
                        "x": 0.1 + landmark * 0.001 + frame_index * 0.05,
                        "y": 0.2 + landmark * 0.001,
                        "visibility": 0.9,
                        "presence": 0.8,
                    }
                    for landmark in range(33)
                ],
            }
        )
    (tmp_path / "mediapipe.json").write_text(
        json.dumps({"frames": frames}), encoding="utf-8"
    )

    async def run():
        async with connect(tmp_path) as session:
            record = await call(
                session,
                "register_pose_reference",
                path="mediapipe.json",
                format="mediapipe",
                width=1000,
                height=500,
                fps=25,
            )
            assert record["adapter"] == "mediapipe-normalized"
            assert len(record["tracks"]) == 33
            assert record["duration"] == 0.04
            wrist = await call(
                session,
                "inspect_motion_reference",
                id=record["id"],
                tracks=["left_wrist"],
            )
            samples = wrist["tracks"]["left_wrist"]["samples"]
            assert samples[0]["x"] == 115
            assert samples[0]["y"] == pytest.approx(107.5)
            assert samples[0]["confidence"] == 0.8
            assert samples[1]["x"] == 165

    asyncio.run(run())


def test_openpose_body25_directory_preserves_missing_frame_confidence(tmp_path):
    frames = tmp_path / "openpose"
    frames.mkdir()
    points = []
    for index in range(25):
        points.extend([100 + index, 200 + index, 0.95])
    (frames / "frame_000000_keypoints.json").write_text(
        json.dumps({"people": [{"pose_keypoints_2d": points}]}), encoding="utf-8"
    )
    (frames / "frame_000001_keypoints.json").write_text(
        json.dumps({"people": []}), encoding="utf-8"
    )

    async def run():
        async with connect(tmp_path) as session:
            record = await call(
                session,
                "register_pose_reference",
                path="openpose",
                format="openpose",
                width=640,
                height=480,
                fps=20,
                coordinate_space="pixels",
            )
            assert record["adapter"] == "openpose-body25"
            assert len(record["source_files"]) == 2
            wrist = await call(
                session,
                "inspect_motion_reference",
                id=record["id"],
                tracks=["right_wrist"],
            )
            samples = wrist["tracks"]["right_wrist"]["samples"]
            assert samples[0] == {
                "time": 0.0,
                "x": 104.0,
                "y": 204.0,
                "confidence": 0.95,
            }
            assert samples[1]["time"] == 0.05
            assert samples[1]["confidence"] == 0

    asyncio.run(run())
