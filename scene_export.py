"""Render a cutout scene through the bundled browser runtime."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread


def _run(args):
    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Scene rendering failed")
    return result.stdout


def _target(work, scene, output):
    if scene["kind"] != "cutout_scene":
        raise ValueError("Choose an editable cutout scene")
    path = work.path(output)
    suffix = path.suffix.lower()
    if suffix not in (".png", ".mp4") or path.is_relative_to(work.store):
        raise ValueError(
            "Choose a workspace .png or .mp4 path outside the material store"
        )
    if not path.parent.is_dir():
        raise ValueError("Output directory must exist")
    return path


def render(work, scene, output, time, fps):
    path = _target(work, scene, output)
    suffix = path.suffix.lower()
    if path.exists():
        raise ValueError("Output directory must exist and output must be a new file")
    node = shutil.which(os.environ.get("REFERENCE_NODE", "node"))
    if not node:
        raise ValueError("Install Node.js or set REFERENCE_NODE to the Node executable")
    if time is not None and (suffix == ".mp4" or not 0 <= time <= scene["duration"]):
        raise ValueError("time applies only to PNG, and must fit the scene duration")
    ffmpeg = (
        shutil.which(os.environ.get("REFERENCE_FFMPEG", "ffmpeg"))
        if suffix == ".mp4"
        else None
    )
    if suffix == ".mp4" and not ffmpeg:
        raise ValueError(
            "MP4 requires FFmpeg on PATH or via REFERENCE_FFMPEG; PNG does not require it"
        )

    script = Path(__file__).with_name("render-cutout.mjs")
    spec = work.path(scene["files"]["scene.json"])
    with TemporaryDirectory(dir=path.parent, prefix=".cutout-render-") as tmp:
        directory = Path(tmp)
        stats = json.loads(
            _run(
                [
                    node,
                    str(script),
                    str(spec),
                    str(directory),
                    suffix,
                    str(time or 0),
                    str(fps),
                ]
            )
        )
        staged = directory / "frame-000000.png"
        if ffmpeg:
            staged = directory / "render.mp4"
            args = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                str(fps),
                "-i",
                str(directory / "frame-%06d.png"),
            ]
            right, bottom = scene["width"] % 2, scene["height"] % 2
            if right or bottom:
                args.extend(
                    [
                        "-vf",
                        f"pad=iw+{right}:ih+{bottom},fillborders=right={right}:bottom={bottom}:mode=smear",
                    ]
                )
            _run(
                [
                    *args,
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(staged),
                ]
            )
        # Exclusive create: leave existing material or exports untouched.
        with staged.open("rb") as source, path.open("xb") as destination:
            try:
                shutil.copyfileobj(source, destination)
            except BaseException:
                destination.close()
                path.unlink(missing_ok=True)
                raise
        files = {"image.png": path.read_bytes()} if suffix == ".png" else {}
    return work.create(
        dict(
            kind="cutout_scene_render",
            origin="rendered",
            parent=scene["id"],
            format=suffix[1:],
            path=work.rel(path),
            sha256=work.digest(path),
            width=scene["width"] + (scene["width"] % 2 if ffmpeg else 0),
            height=scene["height"] + (scene["height"] % 2 if ffmpeg else 0),
            **stats,
        ),
        files,
    )


def _job(work, id):
    record = work.load(id)
    if record["kind"] != "cutout_video_job":
        raise ValueError("Choose a cutout video job")
    return record, work.path(f".reference-materials/{id}/job.json")


def status(work, id):
    record, receipt = _job(work, id)

    def read_state():
        try:
            return json.loads(receipt.read_text("utf-8"))
        except FileNotFoundError:
            return {"status": "queued"}

    state = read_state()
    process = receipt.with_name("worker.pid")
    if state["status"] in ("queued", "running") and process.is_file():
        if not _worker_alive(int(process.read_text("ascii"))):
            state = (
                read_state()
            )  # The worker may have just published its final receipt.
            if state["status"] in ("queued", "running"):
                state = {
                    "status": "interrupted",
                    "error": "Render worker exited without a final receipt",
                }
    return {**record, **state}


def _worker_alive(pid):
    if os.name == "nt":
        from ctypes import WinDLL, wintypes

        kernel = WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _report(work, receipt, status, **result):
    work.put(
        receipt,
        json.dumps({"status": status, **result}, ensure_ascii=False).encode("utf-8"),
    )


def start(work, scene, output, fps):
    path = _target(work, scene, output)
    if path.suffix.lower() != ".mp4":
        raise ValueError("Background export requires an MP4 path")
    record = work.create(
        {
            "kind": "cutout_video_job",
            "scene": scene["id"],
            "output": work.rel(path),
            "fps": fps,
        }
    )
    _, receipt = _job(work, record["id"])
    try:
        receipt.with_name("submitted").touch(exist_ok=False)
    except FileExistsError:
        return status(work, record["id"])
    options = (
        {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("server.py").resolve()),
                "--workspace",
                str(work.root),
                "--render-job",
                record["id"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=work.root,
            **options,
        )
    except OSError as exc:
        _report(work, receipt, "failed", error=str(exc))
    else:
        work.put(receipt.with_name("worker.pid"), str(process.pid).encode("ascii"))
        Thread(target=process.wait, daemon=True).start()
    return status(work, record["id"])


def run_job(work, id):
    record, receipt = _job(work, id)
    _report(work, receipt, "running")
    try:
        result = render(
            work, work.load(record["scene"]), record["output"], None, record["fps"]
        )
    except Exception as exc:
        _report(work, receipt, "failed", error=str(exc)[:4000])
    else:
        _report(work, receipt, "completed", render=result)
