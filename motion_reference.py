"""Import tracked motion and retarget it onto editable scene nodes."""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

import cutout_scene


Key = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Positive = Annotated[FiniteFloat, Field(gt=0)]
Nonnegative = Annotated[FiniteFloat, Field(ge=0)]
Confidence = Annotated[FiniteFloat, Field(ge=0, le=1)]


class MotionSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: Nonnegative
    x: FiniteFloat
    y: FiniteFloat
    confidence: Confidence = 1


class MotionReference(BaseModel):
    """Canonical motion interchange accepted by the plugin.

    Coordinates are image-style pixels: +X is right and +Y is down. Every track
    is independent, so an upstream tracker or agent can export only the landmarks
    required by a shot.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_version: Literal[1] = Field(1, alias="schema")
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    duration: Positive | None = None
    tracks: dict[Key, Annotated[list[MotionSample], Field(min_length=1)]]

    @model_validator(mode="after")
    def complete_duration(self):
        if not self.tracks:
            raise ValueError("A motion reference needs at least one point track")
        latest = 0.0
        for key, samples in self.tracks.items():
            prior = -1.0
            for sample in samples:
                if sample.time <= prior:
                    raise ValueError(
                        f"Motion track {key} times must be strictly increasing"
                    )
                prior = float(sample.time)
                latest = max(latest, prior)
        if self.duration is None:
            if latest <= 0:
                raise ValueError("Inferable motion duration must be positive")
            self.duration = latest
        elif latest > self.duration + 1e-9:
            raise ValueError("Motion samples cannot exceed the reference duration")
        return self


class RetargetBinding(BaseModel):
    """Map tracked points to one local transform track in a cutout scene."""

    model_config = ConfigDict(extra="forbid")
    node: Key
    position: Key | None = None
    direction: tuple[Key, Key] | None = None
    relative_direction: tuple[Key, Key] | None = None
    position_gain: tuple[FiniteFloat, FiniteFloat] = (1, 1)
    rotation_gain: FiniteFloat = 1
    scale_axis: Literal["none", "uniform", "x", "y"] = "none"
    scale_gain: FiniteFloat = 1

    @model_validator(mode="after")
    def useful(self):
        if self.position is None and self.direction is None:
            raise ValueError("A retarget binding needs a position or direction track")
        if self.relative_direction is not None and self.direction is None:
            raise ValueError("A relative direction also needs a primary direction")
        if self.scale_axis != "none" and self.direction is None:
            raise ValueError("Scale retargeting needs a direction pair")
        return self


def _canonical(reference: MotionReference) -> bytes:
    return json.dumps(
        reference.model_dump(by_alias=True),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")


def store(
    work,
    reference: MotionReference,
    *,
    origin: str,
    path: str,
    sha256: str,
    adapter: str | None = None,
    source_files: list[str] | None = None,
) -> dict:
    ranges = {
        key: [float(samples[0].time), float(samples[-1].time)]
        for key, samples in reference.tracks.items()
    }
    record = dict(
        kind="motion_reference",
        origin=origin,
        path=path,
        sha256=sha256,
        media="motion",
        schema=reference.schema_version,
        width=reference.width,
        height=reference.height,
        duration=float(reference.duration),
        tracks=sorted(reference.tracks),
        sample_counts={
            key: len(samples) for key, samples in reference.tracks.items()
        },
        time_ranges=ranges,
    )
    if adapter is not None:
        record["adapter"] = adapter
    if source_files is not None:
        record["source_files"] = source_files
    return work.create(record, {"motion.json": _canonical(reference)})


def register(work, name: str) -> dict:
    path = work.path(name)
    if not path.is_file() or path.is_relative_to(work.store):
        raise ValueError("Select a motion JSON file outside the material store")
    checksum = work.digest(path)
    try:
        reference = MotionReference.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Expected a valid AnimWeaver motion reference JSON") from exc
    if work.digest(path) != checksum:
        raise ValueError("Motion reference changed during registration")
    return store(
        work,
        reference,
        origin="original",
        path=work.rel(path),
        sha256=checksum,
    )


def _load(work, record: dict) -> MotionReference:
    if record["kind"] != "motion_reference":
        raise ValueError("Choose a registered motion reference")
    return MotionReference.model_validate_json(
        work.path(record["files"]["motion.json"]).read_bytes()
    )


def inspect(work, record: dict, tracks: list[str] | None, sample_limit: int) -> dict:
    reference = _load(work, record)
    if not 1 <= sample_limit <= 200:
        raise ValueError("sample_limit must be between 1 and 200")
    selected = tracks or list(reference.tracks)
    unknown = sorted(set(selected) - set(reference.tracks))
    if unknown:
        raise ValueError(f"Unknown motion tracks: {', '.join(unknown)}")
    result = {}
    for key in selected:
        samples = reference.tracks[key]
        if len(samples) <= sample_limit:
            indices = list(range(len(samples)))
        elif sample_limit == 1:
            indices = [0]
        else:
            indices = sorted(
                {
                    round(i * (len(samples) - 1) / (sample_limit - 1))
                    for i in range(sample_limit)
                }
            )
        result[key] = {
            "sample_count": len(samples),
            "time_range": [float(samples[0].time), float(samples[-1].time)],
            "samples": [samples[i].model_dump() for i in indices],
        }
    return {
        "id": record["id"],
        "width": reference.width,
        "height": reference.height,
        "duration": float(reference.duration),
        "tracks": result,
    }


def _track_arrays(reference: MotionReference, key: str, times: np.ndarray):
    try:
        samples = reference.tracks[key]
    except KeyError as exc:
        raise ValueError(f"Unknown motion track: {key}") from exc
    source_times = np.array([float(x.time) for x in samples])
    if times[0] < source_times[0] - 1e-8 or times[-1] > source_times[-1] + 1e-8:
        raise ValueError(
            f"Motion track {key} does not cover {times[0]:.3f}s to {times[-1]:.3f}s"
        )
    x = np.interp(times, source_times, [float(row.x) for row in samples])
    y = np.interp(times, source_times, [float(row.y) for row in samples])
    confidence = np.interp(
        times, source_times, [float(row.confidence) for row in samples]
    )
    return np.column_stack((x, y)), confidence


def _fill_low_confidence(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    if int(valid.sum()) < 2:
        raise ValueError("A mapped motion needs at least two confident samples")
    indices = np.arange(len(values))
    result = values.copy()
    for column in range(values.shape[1]):
        result[:, column] = np.interp(
            indices, indices[valid], values[valid, column]
        )
    return result


def _smooth(values: np.ndarray, samples: int) -> np.ndarray:
    if samples == 0 or len(values) < 3:
        return values
    if samples < 3 or samples % 2 == 0 or samples > 61:
        raise ValueError("smooth_samples must be 0 or an odd number from 3 to 61")
    width = min(samples, len(values) if len(values) % 2 else len(values) - 1)
    if width < 3:
        return values
    pad = width // 2
    padded = np.pad(values, ((pad, pad), (0, 0)), mode="edge")
    kernel = np.ones(width) / width
    return np.column_stack(
        [np.convolve(padded[:, i], kernel, mode="valid") for i in range(values.shape[1])]
    )


def _fit_scale(mode: str, reference: MotionReference, scene: dict):
    sx = scene["width"] / reference.width
    sy = scene["height"] / reference.height
    if mode == "none":
        return 1.0, 1.0
    if mode == "contain":
        value = min(sx, sy)
        return value, value
    if mode == "cover":
        value = max(sx, sy)
        return value, value
    if mode == "stretch":
        return sx, sy
    raise ValueError("fit must be none, contain, cover or stretch")


def _time_grid(start: float, end: float, fps: int) -> np.ndarray:
    if not 1 <= fps <= 120:
        raise ValueError("fps must be between 1 and 120")
    frames = max(2, math.ceil((end - start) * fps) + 1)
    if frames > 7201:
        raise ValueError("Retargeting is limited to 7201 samples per node")
    return np.linspace(start, end, frames)


def _binding_track(
    reference: MotionReference,
    scene: dict,
    binding: RetargetBinding,
    source_times: np.ndarray,
    target_times: np.ndarray,
    fit: str,
    minimum_confidence: float,
    smooth_samples: int,
):
    dependencies = []
    if binding.position:
        dependencies.append(binding.position)
    if binding.direction:
        dependencies.extend(binding.direction)
    if binding.relative_direction:
        dependencies.extend(binding.relative_direction)
    arrays = {}
    confidences = []
    for key in dict.fromkeys(dependencies):
        arrays[key], confidence = _track_arrays(reference, key, source_times)
        confidences.append(confidence)
    valid = np.min(np.stack(confidences), axis=0) >= minimum_confidence
    repaired = int((~valid).sum())
    for key, values in arrays.items():
        arrays[key] = _fill_low_confidence(values, valid)

    fit_x, fit_y = _fit_scale(fit, reference, scene)
    move = np.zeros((len(source_times), 2), dtype=float)
    rotate = np.zeros(len(source_times), dtype=float)
    scale = np.ones((len(source_times), 2), dtype=float)

    if binding.position:
        points = arrays[binding.position]
        delta = points - points[0]
        move[:, 0] = delta[:, 0] * fit_x * float(binding.position_gain[0])
        move[:, 1] = delta[:, 1] * fit_y * float(binding.position_gain[1])

    if binding.direction:
        start_key, end_key = binding.direction
        vector = (arrays[end_key] - arrays[start_key]) * np.array([fit_x, fit_y])
        length = np.linalg.norm(vector, axis=1)
        if length[0] < 1e-6:
            raise ValueError(f"Direction for {binding.node} has zero initial length")
        angle = np.unwrap(np.arctan2(vector[:, 1], vector[:, 0]))
        delta_angle = angle - angle[0]
        if binding.relative_direction:
            rel_start, rel_end = binding.relative_direction
            rel_vector = (arrays[rel_end] - arrays[rel_start]) * np.array(
                [fit_x, fit_y]
            )
            if np.linalg.norm(rel_vector[0]) < 1e-6:
                raise ValueError(
                    f"Relative direction for {binding.node} has zero initial length"
                )
            relative = np.unwrap(np.arctan2(rel_vector[:, 1], rel_vector[:, 0]))
            delta_angle -= relative - relative[0]
        rotate = np.degrees(delta_angle) * float(binding.rotation_gain)
        if binding.scale_axis != "none":
            ratio = 1 + (length / length[0] - 1) * float(binding.scale_gain)
            if np.any(ratio <= 0):
                raise ValueError(f"Scale retargeting for {binding.node} became nonpositive")
            if binding.scale_axis in ("uniform", "x"):
                scale[:, 0] = ratio
            if binding.scale_axis in ("uniform", "y"):
                scale[:, 1] = ratio

    values = np.column_stack((move, rotate, scale))
    # Angles are already unwrapped. Smoothing all transform channels together keeps
    # their phase aligned, then rebases the first sample to a neutral local pose.
    values = _smooth(values, smooth_samples)
    values[:, 0:2] -= values[0, 0:2]
    values[:, 2] -= values[0, 2]
    values[:, 3:5] /= values[0, 3:5]

    poses = []
    for time, row in zip(target_times, values):
        poses.append(
            {
                "time": round(float(time), 9),
                "move": [round(float(row[0]), 6), round(float(row[1]), 6)],
                "rotate": round(float(row[2]), 6),
                "scale": [round(float(row[3]), 6), round(float(row[4]), 6)],
            }
        )
    return poses, repaired


def retarget(
    work,
    scene: dict,
    motion_record: dict,
    bindings: list[RetargetBinding],
    *,
    source_start: float = 0,
    source_end: float | None = None,
    target_start: float = 0,
    target_duration: float | None = None,
    fps: int = 24,
    fit: str = "contain",
    minimum_confidence: float = 0.25,
    smooth_samples: int = 0,
    extend_scene: bool = False,
) -> dict:
    if scene["kind"] != "cutout_scene":
        raise ValueError("Choose an editable cutout scene")
    if not bindings:
        raise ValueError("Provide at least one retarget binding")
    if not 0 <= minimum_confidence <= 1:
        raise ValueError("minimum_confidence must be between 0 and 1")
    if smooth_samples != 0 and (
        smooth_samples < 3 or smooth_samples % 2 == 0 or smooth_samples > 61
    ):
        raise ValueError("smooth_samples must be 0 or an odd number from 3 to 61")
    if source_start < 0 or target_start < 0:
        raise ValueError("Source and target start times must be nonnegative")
    reference = _load(work, motion_record)
    source_end = float(reference.duration) if source_end is None else source_end
    if not source_start < source_end <= float(reference.duration):
        raise ValueError("Source range must have duration and fit the motion reference")
    source_duration = source_end - source_start
    target_duration = source_duration if target_duration is None else target_duration
    if target_duration <= 0:
        raise ValueError("target_duration must be positive")
    target_end = target_start + target_duration
    output_duration = float(scene["duration"])
    if target_end > output_duration + 1e-8:
        if not extend_scene:
            raise ValueError(
                "Retargeted motion exceeds the scene; shorten it or set extend_scene"
            )
        output_duration = target_end

    nodes = {item["key"]: item for item in [*scene["groups"], *scene["layers"]]}
    binding_keys = [binding.node for binding in bindings]
    if len(set(binding_keys)) != len(binding_keys):
        raise ValueError("Retarget bindings must name distinct scene nodes")
    unknown = sorted(set(binding_keys) - set(nodes))
    if unknown:
        raise ValueError(f"Unknown scene nodes: {', '.join(unknown)}")
    for binding in bindings:
        if binding.position is not None and nodes[binding.node].get("mount"):
            raise ValueError(
                f"Mounted group {binding.node} cannot receive translation motion"
            )

    source_times = _time_grid(source_start, source_end, fps)
    target_times = target_start + (
        (source_times - source_start) / source_duration * target_duration
    )
    tracks = {}
    reports = []
    for binding in bindings:
        poses, repaired = _binding_track(
            reference,
            scene,
            binding,
            source_times,
            target_times,
            fit,
            minimum_confidence,
            smooth_samples,
        )
        tracks[binding.node] = poses
        reports.append(
            {
                "node": binding.node,
                "samples": len(poses),
                "repaired_low_confidence_samples": repaired,
            }
        )

    provenance = {
        "reference": motion_record["id"],
        "source_range": [float(source_start), float(source_end)],
        "target_range": [float(target_start), float(target_end)],
        "fps": fps,
        "fit": fit,
        "minimum_confidence": minimum_confidence,
        "smooth_samples": smooth_samples,
        "bindings": [binding.model_dump() for binding in bindings],
        "reports": reports,
    }
    return cutout_scene.revise(
        work,
        scene,
        motions=tracks,
        duration=output_duration if output_duration != scene["duration"] else None,
        provenance=provenance,
    )
