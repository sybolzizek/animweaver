"""Deterministic visual checks for an editable cutout scene."""

import json
import os
import shutil
import subprocess
from pathlib import Path


def audit(work, scene: dict, fps: int, max_frames: int, pop_ratio: float) -> dict:
    if scene["kind"] != "cutout_scene":
        raise ValueError("Choose an editable cutout scene")
    if not 1 <= fps <= 60:
        raise ValueError("Audit fps must be between 1 and 60")
    if not 2 <= max_frames <= 1200:
        raise ValueError("max_frames must be between 2 and 1200")
    if not 1.5 <= pop_ratio <= 20:
        raise ValueError("pop_ratio must be between 1.5 and 20")
    node = shutil.which(os.environ.get("REFERENCE_NODE", "node"))
    if node is None:
        raise ValueError("Scene auditing needs Node.js or REFERENCE_NODE")
    script = Path(__file__).with_name("audit-cutout.mjs")
    if not script.is_file():
        raise ValueError("The installed plugin is missing its scene audit runtime")
    result = subprocess.run(
        [
            node,
            str(script),
            str(work.path(scene["files"]["scene.json"])),
            str(fps),
            str(max_frames),
            str(pop_ratio),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Scene auditing failed")
    report = json.loads(result.stdout)
    report["scene"] = scene["id"]
    payload = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    return work.create(
        dict(
            kind="cutout_scene_audit",
            origin="derived",
            parent=scene["id"],
            scene=scene["id"],
            fps=fps,
            max_frames=max_frames,
            pop_ratio=pop_ratio,
            frames_sampled=report["frames_sampled"],
            effective_fps=report["effective_fps"],
            max_exposed=report["max_exposed"],
            max_unfilled=report["max_unfilled"],
            seam_report=report["seam_report"],
            pop_candidates=report["pop_candidates"],
            warnings=report["warnings"],
        ),
        {"audit.json": payload},
    )
