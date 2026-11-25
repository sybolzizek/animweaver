"""JSON bridge to the TypeScript scene planner. No image data crosses it."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from pydantic_core import to_jsonable_python


ROOT = Path(__file__).resolve().parent


def plan_scene(scene, patch=None):
    node = shutil.which(os.environ.get("REFERENCE_NODE", "node"))
    if node is None:
        raise ValueError("Scene planning needs Node.js or REFERENCE_NODE")
    if (
        not (ROOT / "scene-plan.mjs").is_file()
        or not (ROOT / "cutout-timeline.mjs").is_file()
    ):
        raise ValueError(
            "Scene runtime is not built. Run npm ci && npm run build in the plugin directory"
        )
    payload = {"scene": scene}
    if patch is not None:
        payload["patch"] = {
            name: value for name, value in patch.items() if value is not None
        }
    result = subprocess.run(
        [node, str(ROOT / "scene-planner-cli.mjs")],
        input=json.dumps(to_jsonable_python(payload), allow_nan=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Scene planning failed")
    return json.loads(result.stdout)
