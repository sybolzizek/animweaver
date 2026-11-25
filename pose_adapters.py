"""Adapters from common pose-tracker JSON into AnimWeaver point tracks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import motion_reference


MEDIAPIPE_33 = [
    "nose",
    "left_eye_inner",
    "left_eye",
    "left_eye_outer",
    "right_eye_inner",
    "right_eye",
    "right_eye_outer",
    "left_ear",
    "right_ear",
    "mouth_left",
    "mouth_right",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_pinky",
    "right_pinky",
    "left_index",
    "right_index",
    "left_thumb",
    "right_thumb",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
    "left_heel",
    "right_heel",
    "left_foot_index",
    "right_foot_index",
]

OPENPOSE_BODY_25 = [
    "nose",
    "neck",
    "right_shoulder",
    "right_elbow",
    "right_wrist",
    "left_shoulder",
    "left_elbow",
    "left_wrist",
    "mid_hip",
    "right_hip",
    "right_knee",
    "right_ankle",
    "left_hip",
    "left_knee",
    "left_ankle",
    "right_eye",
    "left_eye",
    "right_ear",
    "left_ear",
    "left_big_toe",
    "left_small_toe",
    "left_heel",
    "right_big_toe",
    "right_small_toe",
    "right_heel",
]

OPENPOSE_COCO_18 = [
    "nose",
    "neck",
    "right_shoulder",
    "right_elbow",
    "right_wrist",
    "left_shoulder",
    "left_elbow",
    "left_wrist",
    "right_hip",
    "right_knee",
    "right_ankle",
    "left_hip",
    "left_knee",
    "left_ankle",
    "right_eye",
    "left_eye",
    "right_ear",
    "left_ear",
]


def _confidence(landmark) -> float:
    if isinstance(landmark, dict):
        values = [
            landmark[key]
            for key in ("visibility", "presence", "confidence", "score")
            if key in landmark
        ]
    else:
        values = list(landmark[3:5]) if len(landmark) > 3 else []
    return max(0.0, min(1.0, float(min(values) if values else 1)))


def _xy(landmark):
    if isinstance(landmark, dict):
        return float(landmark["x"]), float(landmark["y"])
    if not isinstance(landmark, list) or len(landmark) < 2:
        raise ValueError("Each landmark needs x and y")
    return float(landmark[0]), float(landmark[1])


def _frame_time(frame, index: int, fps: float) -> float:
    if isinstance(frame, dict) and "time" in frame:
        return float(frame["time"])
    if isinstance(frame, dict) and "timestamp_ms" in frame:
        return float(frame["timestamp_ms"]) / 1000
    return index / fps


def _mediapipe_person(frame, person_index: int):
    if not isinstance(frame, dict):
        raise ValueError("Each MediaPipe frame must be an object")
    landmarks = frame.get("pose_landmarks", frame.get("landmarks"))
    if landmarks is None:
        return None
    if not isinstance(landmarks, list):
        raise ValueError("MediaPipe landmarks must be a list")
    if (
        landmarks
        and isinstance(landmarks[0], list)
        and landmarks[0]
        and isinstance(landmarks[0][0], (dict, list))
    ):
        if person_index >= len(landmarks):
            return None
        landmarks = landmarks[person_index]
    return landmarks


def _mediapipe_reference(
    payload, width: int, height: int, fps: float, person_index: int, coordinate_space: str
):
    frames = payload.get("frames") if isinstance(payload, dict) else payload
    if not isinstance(frames, list) or len(frames) < 2:
        raise ValueError("MediaPipe input needs a frames list with at least two frames")
    tracks = {name: [] for name in MEDIAPIPE_33}
    prior = -1.0
    observed = False
    for index, frame in enumerate(frames):
        time = _frame_time(frame, index, fps)
        if time <= prior:
            raise ValueError("MediaPipe frame times must be strictly increasing")
        prior = time
        person = _mediapipe_person(frame, person_index)
        if person is not None and len(person) != len(MEDIAPIPE_33):
            raise ValueError("MediaPipe Pose uses exactly 33 landmarks per person")
        observed |= person is not None
        for landmark_index, name in enumerate(MEDIAPIPE_33):
            if person is None:
                x = y = confidence = 0.0
            else:
                x, y = _xy(person[landmark_index])
                confidence = _confidence(person[landmark_index])
                if coordinate_space == "normalized":
                    x, y = x * width, y * height
            tracks[name].append(
                {"time": time, "x": x, "y": y, "confidence": confidence}
            )
    if not observed:
        raise ValueError("No MediaPipe person was found at the selected index")
    return motion_reference.MotionReference.model_validate(
        {"schema": 1, "width": width, "height": height, "duration": prior, "tracks": tracks}
    )


def _openpose_layout(payloads, person_index: int):
    lengths = set()
    for payload in payloads:
        people = payload.get("people", [])
        if person_index < len(people):
            points = people[person_index].get("pose_keypoints_2d", [])
            if points:
                if len(points) % 3:
                    raise ValueError("OpenPose keypoints must be flattened x,y,confidence triples")
                lengths.add(len(points) // 3)
    if not lengths:
        raise ValueError("No OpenPose person was found at the selected index")
    if len(lengths) != 1:
        raise ValueError("OpenPose files use inconsistent body layouts")
    count = lengths.pop()
    if count == 25:
        return OPENPOSE_BODY_25, "openpose-body25"
    if count == 18:
        return OPENPOSE_COCO_18, "openpose-coco18"
    raise ValueError("Only OpenPose BODY_25 and COCO_18 pose layouts are supported")


def _openpose_reference(payloads, width: int, height: int, fps: float, person_index: int):
    names, adapter = _openpose_layout(payloads, person_index)
    tracks = {name: [] for name in names}
    for frame_index, payload in enumerate(payloads):
        people = payload.get("people", [])
        points = (
            people[person_index].get("pose_keypoints_2d", [])
            if person_index < len(people)
            else []
        )
        if points and len(points) != len(names) * 3:
            raise ValueError("OpenPose frame does not match the detected body layout")
        time = frame_index / fps
        for point_index, name in enumerate(names):
            if not points:
                x = y = confidence = 0.0
            else:
                x, y, confidence = map(
                    float, points[point_index * 3 : point_index * 3 + 3]
                )
                confidence = max(0.0, min(1.0, confidence))
            tracks[name].append(
                {"time": time, "x": x, "y": y, "confidence": confidence}
            )
    return (
        motion_reference.MotionReference.model_validate(
            {
                "schema": 1,
                "width": width,
                "height": height,
                "duration": (len(payloads) - 1) / fps,
                "tracks": tracks,
            }
        ),
        adapter,
    )


def _combined_digest(files: list[Path], payloads: list[bytes]) -> str:
    digest = hashlib.sha256()
    for path, payload in zip(files, payloads):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def register(
    work,
    name: str,
    format: str,
    width: int,
    height: int,
    fps: float,
    person_index: int,
    coordinate_space: str,
) -> dict:
    if width <= 0 or height <= 0:
        raise ValueError("Pose reference width and height must be positive")
    if not 0 < fps <= 240:
        raise ValueError("Pose reference fps must be greater than 0 and at most 240")
    if person_index < 0:
        raise ValueError("person_index must be nonnegative")
    if coordinate_space not in ("normalized", "pixels"):
        raise ValueError("coordinate_space must be normalized or pixels")
    path = work.path(name)
    if path.is_relative_to(work.store):
        raise ValueError("Select pose tracker output outside the material store")

    if format == "mediapipe":
        if not path.is_file():
            raise ValueError("MediaPipe input must be one JSON file")
        files = [path]
    elif format == "openpose":
        if not path.is_dir():
            raise ValueError("OpenPose input must be a directory of JSON frames")
        files = sorted(path.glob("*.json"))
        if len(files) < 2:
            raise ValueError("OpenPose input needs at least two JSON frame files")
    else:
        raise ValueError("format must be mediapipe or openpose")

    payload_bytes = [file.read_bytes() for file in files]
    try:
        payloads = [json.loads(payload) for payload in payload_bytes]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Pose tracker output contains invalid JSON") from exc
    checksum = _combined_digest(files, payload_bytes)

    if format == "mediapipe":
        reference = _mediapipe_reference(
            payloads[0], width, height, fps, person_index, coordinate_space
        )
        adapter = f"mediapipe-{coordinate_space}"
    else:
        reference, adapter = _openpose_reference(
            payloads, width, height, fps, person_index
        )

    if any(file.read_bytes() != payload for file, payload in zip(files, payload_bytes)):
        raise ValueError("Pose tracker output changed during registration")
    return motion_reference.store(
        work,
        reference,
        origin="derived",
        path=work.rel(path),
        sha256=checksum,
        adapter=adapter,
        source_files=[work.rel(file) for file in files],
    )
