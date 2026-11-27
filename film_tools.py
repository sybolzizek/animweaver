"""MCP entry points for frame-exact multi-shot AnimWeaver film projects."""

import json

import film_export
import film_project


def register_film_tools(mcp, work):
    @mcp.resource(
        "reference-materials://schema/film-project",
        mime_type="application/schema+json",
    )
    def film_project_schema() -> str:
        """Machine-readable authoring contract for a multi-shot film."""
        return json.dumps(
            film_project.FilmRecipe.model_json_schema(), ensure_ascii=False, indent=2
        )

    @mcp.tool()
    def register_audio(path: str) -> dict:
        """Snapshot a PCM WAV soundtrack or stem from the workspace.

        The registered audio retains duration, sample rate, channel count and SHA-256.
        Film projects refer to this immutable snapshot rather than a mutable file path.
        """
        return film_project.register_audio(work, path)

    @mcp.tool()
    def create_film_project(
        shots: list[film_project.FilmShot],
        fps: int = 24,
        audio: str | None = None,
        tempo_bpm: float | None = None,
        beats_per_bar: int = 4,
        events: list[film_project.FilmEvent] | None = None,
    ) -> dict:
        """Create an editable, frame-exact film from ordered cutout-scene ranges.

        Each shot supplies a stable key, scene ID, source start and optional exact
        frame count. Omitted frame counts consume the remaining scene duration.
        Version 1 uses contiguous hard cuts and requires one canvas size. Optional
        tempo records beat alignment; events are frame-addressed action or sound cues.
        Audio must be a registered PCM WAV snapshot. This tool compiles the timeline
        but does not render it.
        """
        return film_project.build(
            work,
            film_project.FilmRecipe(
                fps=fps,
                shots=shots,
                audio=audio,
                tempo_bpm=tempo_bpm,
                beats_per_bar=beats_per_bar,
                events=events or [],
            ),
        )

    @mcp.tool()
    def revise_film_project(
        id: str,
        fps: int | None = None,
        shots: list[film_project.FilmShot] | None = None,
        audio: str | None = None,
        replace_audio: bool = False,
        tempo_bpm: float | None = None,
        replace_tempo: bool = False,
        beats_per_bar: int | None = None,
        events: list[film_project.FilmEvent] | None = None,
    ) -> dict:
        """Create a revised immutable film timeline while reusing its scenes.

        Omitted fields remain unchanged. Set replace_audio with a null audio value to
        remove the soundtrack, or replace_tempo with null tempo_bpm to remove the beat
        grid. Shot ranges and events are revalidated at the resulting frame rate.
        """
        return film_project.revise(
            work,
            work.load(id),
            fps=fps,
            shots=shots,
            audio=audio,
            replace_audio=replace_audio,
            tempo_bpm=tempo_bpm,
            replace_tempo=replace_tempo,
            beats_per_bar=beats_per_bar,
            events=events,
        )

    @mcp.tool()
    def inspect_film_timeline(id: str) -> dict:
        """Read compiled shot frames, global events, beat offsets and warnings."""
        return film_project.inspect(work, work.load(id))

    @mcp.tool()
    def render_film_project(id: str, output: str) -> dict:
        """Submit a complete film project for MP4 rendering.

        Every shot is rendered from its editable scene at the project's exact fps.
        Export rejects transparent frames, verifies the total frame count, reports
        per-shot seam status and muxes registered audio when present. Use
        inspect_film_video with the returned job ID to observe completion.
        """
        return film_export.start(work, work.load(id), output)

    @mcp.tool()
    def inspect_film_video(id: str) -> dict:
        """Read a submitted film render job without restarting it."""
        return film_export.status(work, id)
