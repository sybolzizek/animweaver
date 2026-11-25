"""MCP entry points for agent-authored motion references and retargeting."""

import json
from typing import Literal

import motion_reference
import pose_adapters


def register_motion_tools(mcp, work):
    @mcp.resource(
        "reference-materials://schema/motion-reference",
        mime_type="application/schema+json",
    )
    def motion_reference_schema() -> str:
        """Machine-readable schema for tracked 2D point motion."""
        return json.dumps(
            motion_reference.MotionReference.model_json_schema(by_alias=True),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool()
    def register_motion_reference(path: str) -> dict:
        """Register tracked 2D point motion from a workspace JSON file.

        Schema 1 uses width, height, duration and a tracks object. Each track is
        an ordered list of {time, x, y, confidence}; coordinates use image pixels
        with +X right and +Y down. The tool stores a canonical local snapshot so
        later retargeting is reproducible and independent of the source file.
        """
        return motion_reference.register(work, path)

    @mcp.tool()
    def register_pose_reference(
        path: str,
        format: Literal["mediapipe", "openpose"],
        width: int,
        height: int,
        fps: float = 30,
        person_index: int = 0,
        coordinate_space: Literal["normalized", "pixels"] = "normalized",
    ) -> dict:
        """Convert common pose-tracker JSON into a registered motion reference.

        MediaPipe accepts one JSON file containing a frames list; each frame uses
        pose_landmarks or landmarks with one 33-landmark pose, or a list of poses.
        OpenPose accepts a directory of lexically ordered per-frame JSON files and
        supports BODY_25 or COCO_18 pose_keypoints_2d. Width and height describe the
        tracked image. OpenPose points are already pixels; coordinate_space applies
        to MediaPipe. Missing people or landmarks become zero-confidence samples so
        retargeting can report and interpolate them rather than inventing certainty.
        """
        return pose_adapters.register(
            work,
            path,
            format,
            width,
            height,
            fps,
            person_index,
            coordinate_space,
        )

    @mcp.tool()
    def inspect_motion_reference(
        id: str, tracks: list[str] | None = None, sample_limit: int = 12
    ) -> dict:
        """Inspect selected tracked points without returning an unbounded motion file."""
        return motion_reference.inspect(work, work.load(id), tracks, sample_limit)

    @mcp.tool()
    def retarget_motion_reference(
        scene_id: str,
        motion_id: str,
        bindings: list[motion_reference.RetargetBinding],
        source_start: float = 0,
        source_end: float | None = None,
        target_start: float = 0,
        target_duration: float | None = None,
        fps: int = 24,
        fit: Literal["none", "contain", "cover", "stretch"] = "contain",
        minimum_confidence: float = 0.25,
        smooth_samples: int = 0,
        extend_scene: bool = False,
    ) -> dict:
        """Retarget tracked 2D points onto editable scene-node motion tracks.

        position drives local translation relative to the first source sample.
        direction=[from,to] drives rotation and optional scale from that vector.
        relative_direction subtracts another vector's rotation, which is useful
        for a child bone whose motion should stay local to its parent. Only mapped
        nodes are replaced; all rasters, meshes, layering and other tracks remain.
        Low-confidence samples are interpolated and counted in the result record.
        Set extend_scene only when the mapped target range should lengthen the shot.
        """
        return motion_reference.retarget(
            work,
            work.load(scene_id),
            work.load(motion_id),
            bindings,
            source_start=source_start,
            source_end=source_end,
            target_start=target_start,
            target_duration=target_duration,
            fps=fps,
            fit=fit,
            minimum_confidence=minimum_confidence,
            smooth_samples=smooth_samples,
            extend_scene=extend_scene,
        )
