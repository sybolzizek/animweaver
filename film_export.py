"""Render frame-exact AnimWeaver film projects through the bundled browser runtime."""

from __future__ import annotations

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
        raise ValueError(result.stderr.strip() or "Film rendering failed")
    return result.stdout


def _target(work, project: dict, output: str):
    if project.get("kind") != "film_project":
        raise ValueError("Choose an editable film project")
    path = work.path(output)
    if path.suffix.lower() != ".mp4" or path.is_relative_to(work.store):
        raise ValueError("Choose a workspace .mp4 path outside the material store")
    if not path.parent.is_dir():
        raise ValueError("The film output directory must exist")
    return path


def render(work, project: dict, output: str) -> dict:
    path = _target(work, project, output)
    if path.exists():
        raise ValueError("Film output must be a new file")
    node = shutil.which(os.environ.get("REFERENCE_NODE", "node"))
    ffmpeg = shutil.which(os.environ.get("REFERENCE_FFMPEG", "ffmpeg"))
    if not node:
        raise ValueError("Install Node.js or set REFERENCE_NODE to the Node executable")
    if not ffmpeg:
        raise ValueError("Film export requires FFmpeg or REFERENCE_FFMPEG")

    film = json.loads(work.path(project["files"]["film.json"]).read_text("utf-8"))
    shots = []
    for shot in film["shots"]:
        scene = work.load(shot["scene"])
        if scene.get("kind") != "cutout_scene":
            raise ValueError(f"Film shot {shot['key']} no longer references a scene")
        shots.append(
            {
                **shot,
                "spec_file": str(work.path(scene["files"]["scene.json"])),
            }
        )
    audio = work.load(film["audio"]) if film.get("audio") else None
    audio_path = (
        work.path(audio["files"]["audio.wav"])
        if audio is not None
        else None
    )

    script = Path(__file__).with_name("render-film.mjs")
    with TemporaryDirectory(dir=path.parent, prefix=".film-render-") as tmp:
        directory = Path(tmp)
        job = {
            "width": film["width"],
            "height": film["height"],
            "fps": film["fps"],
            "frames": film["frames"],
            "shots": shots,
        }
        job_file = directory / "job.json"
        job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        stats = json.loads(_run([node, str(script), str(job_file), str(directory)]))
        staged = directory / "render.mp4"
        right, bottom = film["width"] % 2, film["height"] % 2
        video = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(film["fps"]),
            "-i",
            str(directory / "frame-%06d.png"),
        ]
        if audio_path is not None:
            video.extend(["-i", str(audio_path)])
        video.extend(["-map", "0:v:0"])
        if audio_path is not None:
            video.extend(["-map", "1:a:0", "-af", "apad"])
        if right or bottom:
            video.extend(
                [
                    "-vf",
                    f"pad=iw+{right}:ih+{bottom},fillborders=right={right}:bottom={bottom}:mode=smear",
                ]
            )
        video.extend(
            [
                "-frames:v",
                str(film["frames"]),
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "16",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-color_range",
                "tv",
                "-colorspace",
                "bt709",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
            ]
        )
        if audio_path is not None:
            video.extend(
                [
                    "-c:a",
                    "aac",
                    "-b:a",
                    "320k",
                    "-t",
                    f"{film['duration']:.9f}",
                ]
            )
        else:
            video.append("-an")
        video.extend(["-movflags", "+faststart", str(staged)])
        _run(video)
        with staged.open("rb") as source, path.open("xb") as destination:
            try:
                shutil.copyfileobj(source, destination)
            except BaseException:
                destination.close()
                path.unlink(missing_ok=True)
                raise
    return work.create(
        dict(
            kind="film_render",
            origin="rendered",
            parent=project["id"],
            project=project["id"],
            format="mp4",
            path=work.rel(path),
            sha256=work.digest(path),
            width=film["width"] + right,
            height=film["height"] + bottom,
            audio=film.get("audio"),
            **stats,
        )
    )


def _job(work, ref_id: str):
    record = work.load(ref_id)
    if record.get("kind") != "film_video_job":
        raise ValueError("Choose a film video job")
    return record, work.path(f".reference-materials/{ref_id}/job.json")


def _worker_alive(pid: int):
    if os.name == "nt":
        from ctypes import WinDLL, wintypes

        kernel = WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
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


def _report(work, receipt: Path, status: str, **result):
    work.put(
        receipt,
        json.dumps({"status": status, **result}, ensure_ascii=False).encode("utf-8"),
    )


def status(work, ref_id: str) -> dict:
    record, receipt = _job(work, ref_id)

    def read_state():
        try:
            return json.loads(receipt.read_text("utf-8"))
        except FileNotFoundError:
            return {"status": "queued"}

    state = read_state()
    process = receipt.with_name("worker.pid")
    if state["status"] in ("queued", "running") and process.is_file():
        if not _worker_alive(int(process.read_text("ascii"))):
            state = read_state()
            if state["status"] in ("queued", "running"):
                state = {
                    "status": "interrupted",
                    "error": "Film render worker exited without a final receipt",
                }
    return {**record, **state}


def start(work, project: dict, output: str) -> dict:
    path = _target(work, project, output)
    record = work.create(
        {
            "kind": "film_video_job",
            "project": project["id"],
            "output": work.rel(path),
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
                "--film-render-job",
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


def run_job(work, ref_id: str):
    record, receipt = _job(work, ref_id)
    _report(work, receipt, "running")
    try:
        result = render(
            work,
            work.load(record["project"]),
            record["output"],
        )
    except Exception as exc:
        _report(work, receipt, "failed", error=str(exc)[:4000])
    else:
        _report(work, receipt, "completed", render=result)
