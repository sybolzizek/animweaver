"""Frame-exact multi-shot film projects built from editable AnimWeaver scenes."""

from __future__ import annotations

import json
import math
import re
import wave
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


MATERIAL_ID = re.compile(r"[0-9a-f]{24}")
Key = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Positive = Annotated[FiniteFloat, Field(gt=0)]


class FilmShot(BaseModel):
    """One contiguous range from an editable scene, measured in output frames."""

    model_config = ConfigDict(extra="forbid")
    key: Key
    scene: str
    source_start: Annotated[FiniteFloat, Field(ge=0)] = 0
    frames: Annotated[int, Field(ge=1, le=1_000_000)] | None = None


class FilmEvent(BaseModel):
    """A frame-addressed action or sound-design cue inside one shot."""

    model_config = ConfigDict(extra="forbid")
    shot: Key
    frame: Annotated[int, Field(ge=0)]
    kind: Key
    duration_frames: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    intensity: Annotated[FiniteFloat, Field(ge=0, le=4)] = 1
    note: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def clean_note(self):
        if self.note is not None:
            self.note = self.note.strip() or None
        return self


class FilmRecipe(BaseModel):
    """Authoring contract for a film project; cuts are contiguous in version 1."""

    model_config = ConfigDict(extra="forbid")
    fps: Annotated[int, Field(ge=1, le=120)] = 24
    shots: Annotated[list[FilmShot], Field(min_length=1, max_length=10_000)]
    audio: str | None = None
    tempo_bpm: Positive | None = None
    beats_per_bar: Annotated[int, Field(ge=1, le=32)] = 4
    events: list[FilmEvent] = Field(default_factory=list)


def register_audio(work, name: str) -> dict:
    """Snapshot a PCM WAV source so a film and its package remain reproducible."""

    path = work.path(name)
    if (
        not path.is_file()
        or path.is_relative_to(work.store)
        or path.suffix.lower() != ".wav"
    ):
        raise ValueError("Select a PCM WAV file outside the material store")
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("Audio source is larger than 512 MiB")
    checksum = work.digest(path)
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            frames = source.getnframes()
            sample_width = source.getsampwidth()
            compression = source.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("Expected a readable PCM WAV file") from exc
    if compression != "NONE" or channels < 1 or sample_rate < 1 or sample_width < 1:
        raise ValueError("Expected uncompressed PCM WAV audio")
    if work.digest(path) != checksum:
        raise ValueError("Audio changed during registration")
    return work.create(
        dict(
            kind="audio_source",
            origin="original",
            path=work.rel(path),
            sha256=checksum,
            media="audio",
            container="wav",
            channels=channels,
            sample_rate=sample_rate,
            sample_width_bits=sample_width * 8,
            frames=frames,
            duration=frames / sample_rate,
        ),
        {"audio.wav": path.read_bytes()},
    )


def _frames_for(scene: dict, shot: FilmShot, fps: int) -> int:
    available = float(scene["duration"]) - float(shot.source_start)
    if available <= 0:
        raise ValueError(f"Shot {shot.key} starts outside scene {shot.scene}")
    frames = shot.frames
    if frames is None:
        frames = max(1, math.ceil(available * fps - 1e-9))
    if frames / fps > available + 1e-9:
        raise ValueError(
            f"Shot {shot.key} requests {frames} frames beyond scene {shot.scene}"
        )
    return frames


def _beat_summary(shots: list[dict], fps: int, tempo_bpm: float | None, beats: int):
    if tempo_bpm is None:
        return None
    frames_per_beat = fps * 60 / tempo_bpm
    boundaries = []
    for shot in shots:
        beat = shot["start_frame"] / frames_per_beat
        nearest = round(beat)
        boundaries.append(
            {
                "shot": shot["key"],
                "frame": shot["start_frame"],
                "beat": beat,
                "nearest_beat": nearest,
                "offset_frames": shot["start_frame"] - nearest * frames_per_beat,
                "bar": math.floor(beat / beats) + 1,
            }
        )
    return {
        "tempo_bpm": tempo_bpm,
        "beats_per_bar": beats,
        "frames_per_beat": frames_per_beat,
        "boundaries": boundaries,
    }


def build(work, recipe: FilmRecipe, previous: str | None = None) -> dict:
    """Compile ordered scene ranges into an immutable, frame-exact film record."""

    seen = set()
    compiled = []
    scenes = {}
    width = height = None
    cursor = 0
    for authored in recipe.shots:
        if authored.key in seen:
            raise ValueError(f"Duplicate film shot key {authored.key}")
        seen.add(authored.key)
        if not MATERIAL_ID.fullmatch(authored.scene):
            raise ValueError(f"Shot {authored.key} has an invalid scene ID")
        scene = work.load(authored.scene)
        if scene.get("kind") != "cutout_scene":
            raise ValueError(f"Shot {authored.key} must reference an editable scene")
        if width is None:
            width, height = scene["width"], scene["height"]
        elif (scene["width"], scene["height"]) != (width, height):
            raise ValueError("All film scenes must use the same canvas dimensions")
        frames = _frames_for(scene, authored, recipe.fps)
        row = {
            "key": authored.key,
            "scene": authored.scene,
            "source_start": float(authored.source_start),
            "source_end": float(authored.source_start) + frames / recipe.fps,
            "start_frame": cursor,
            "frames": frames,
        }
        compiled.append(row)
        scenes[authored.key] = scene
        cursor += frames

    audio = None
    if recipe.audio is not None:
        if not MATERIAL_ID.fullmatch(recipe.audio):
            raise ValueError("Film audio has an invalid material ID")
        audio = work.load(recipe.audio)
        if audio.get("kind") != "audio_source" or "audio.wav" not in audio["files"]:
            raise ValueError("Film audio must be a registered PCM WAV source")

    by_key = {shot["key"]: shot for shot in compiled}
    events = []
    for authored in recipe.events:
        shot = by_key.get(authored.shot)
        if shot is None:
            raise ValueError(f"Film event references unknown shot {authored.shot}")
        if authored.frame >= shot["frames"]:
            raise ValueError(f"Film event {authored.kind} falls outside shot {authored.shot}")
        if authored.frame + authored.duration_frames > shot["frames"]:
            raise ValueError(f"Film event {authored.kind} extends beyond shot {authored.shot}")
        events.append(
            authored.model_dump()
            | {"global_frame": shot["start_frame"] + authored.frame}
        )
    events.sort(key=lambda event: (event["global_frame"], event["kind"]))

    beat = _beat_summary(
        compiled,
        recipe.fps,
        float(recipe.tempo_bpm) if recipe.tempo_bpm is not None else None,
        recipe.beats_per_bar,
    )
    duration = cursor / recipe.fps
    warnings = []
    if audio is not None and audio["duration"] + 1 / recipe.fps < duration:
        warnings.append(
            {
                "code": "audio_shorter_than_picture",
                "audio_seconds": audio["duration"],
                "picture_seconds": duration,
            }
        )
    if beat is not None:
        off_beat = [
            row
            for row in beat["boundaries"][1:]
            if abs(row["offset_frames"]) > 0.25
        ]
        if off_beat:
            warnings.append(
                {
                    "code": "shot_boundaries_off_beat",
                    "shots": [row["shot"] for row in off_beat],
                }
            )

    spec = {
        "schema": 1,
        "width": width,
        "height": height,
        "fps": recipe.fps,
        "frames": cursor,
        "duration": duration,
        "shots": compiled,
        "events": events,
        "audio": recipe.audio,
        "beat": beat,
        "warnings": warnings,
    }
    inputs = {f"scene_{shot['key']}": shot["scene"] for shot in compiled}
    if recipe.audio is not None:
        inputs["audio"] = recipe.audio
    record = {
        "kind": "film_project",
        "origin": "derived",
        **({"parent": previous} if previous else {}),
        "inputs": inputs,
        **spec,
        "tempo_bpm": spec["beat"]["tempo_bpm"] if spec["beat"] else None,
        "beats_per_bar": recipe.beats_per_bar,
    }
    return work.create(
        record,
        {"film.json": json.dumps(spec, ensure_ascii=False, indent=2).encode("utf-8")},
    )


def revise(
    work,
    project: dict,
    *,
    fps: int | None = None,
    shots: list[FilmShot] | None = None,
    audio: str | None = None,
    replace_audio: bool = False,
    tempo_bpm: float | None = None,
    replace_tempo: bool = False,
    beats_per_bar: int | None = None,
    events: list[FilmEvent] | None = None,
) -> dict:
    if project.get("kind") != "film_project":
        raise ValueError("Select an editable film project")
    current_shots = [
        FilmShot(
            key=row["key"],
            scene=row["scene"],
            source_start=row["source_start"],
            frames=row["frames"],
        )
        for row in project["shots"]
    ]
    current_events = [
        FilmEvent(**{key: value for key, value in row.items() if key != "global_frame"})
        for row in project.get("events", [])
    ]
    recipe = FilmRecipe(
        fps=fps if fps is not None else project["fps"],
        shots=shots if shots is not None else current_shots,
        audio=audio if replace_audio else project.get("audio"),
        tempo_bpm=tempo_bpm if replace_tempo else project.get("tempo_bpm"),
        beats_per_bar=(
            beats_per_bar if beats_per_bar is not None else project["beats_per_bar"]
        ),
        events=events if events is not None else current_events,
    )
    return build(work, recipe, previous=project["id"])


def inspect(work, project: dict) -> dict:
    if project.get("kind") != "film_project":
        raise ValueError("Select an editable film project")
    return json.loads(work.path(project["files"]["film.json"]).read_text("utf-8"))
