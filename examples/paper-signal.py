"""Rebuild an original still as an editable scene over MCP stdio; no downloads."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image, ImageDraw


SIZE = (640, 360)


def draw_sources(root):
    width, height = SIZE
    plate = Image.new("RGB", SIZE)
    sky = ImageDraw.Draw(plate)
    for y in range(height):
        sky.line((0, y, width, y), fill=(15 + y // 24, 33 + y // 14, 52 + y // 12))
    for i in range(100):
        x, y = ((i * 367 + 19) % width, (i * i * 53 + 97) % 260)
        sky.point((x, y), fill=(65 + i % 87, 91 + i % 87, 112 + i % 87))
    sky.polygon(
        [
            (0, 265),
            (105, 228),
            (199, 280),
            (309, 230),
            (442, 282),
            (527, 262),
            (640, 295),
            (640, 360),
            (0, 360),
        ],
        fill="#263d4d",
    )
    sky.polygon(
        [
            (0, 305),
            (135, 280),
            (254, 322),
            (430, 270),
            (640, 316),
            (640, 360),
            (0, 360),
        ],
        fill="#152a39",
    )
    plate.save(root / "backing.png")

    signal_mask = Image.new("L", SIZE)
    ImageDraw.Draw(signal_mask).ellipse((214, 111, 289, 186), fill=255)
    signal_mask.save(root / "signal-mask.png")
    signal = Image.new("RGB", SIZE, "#f7bb72")
    rings = ImageDraw.Draw(signal)
    for radius in (12, 21, 30):
        rings.ellipse(
            (252 - radius, 149 - radius, 252 + radius, 149 + radius),
            outline="#a45644",
            width=1,
        )
    rings.ellipse((248, 145, 256, 153), fill="#fbda9b")

    gantry = Image.new("RGBA", SIZE)
    steel = ImageDraw.Draw(gantry)
    for x in (334, 399):
        steel.rectangle((x, 71, x + 4, 330), fill="#6c9a9a")
        steel.ellipse((x - 2, 67, x + 6, 75), fill="#b4c3a2")
    for y in range(95, 310, 56):
        steel.line((336, y, 401, y + 55, 336, y + 55, 401, y), fill="#548184", width=3)
    steel.rectangle((308, 330, 427, 337), fill="#6c9a9a")
    gantry.getchannel("A").save(root / "foreground-mask.png")

    still = plate.convert("RGBA")
    still.paste(signal, mask=signal_mask)
    still.alpha_composite(gantry)
    still.save(root / "still.png")


async def make_scene(root, video):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            str(Path(__file__).resolve().parents[1] / "server.py"),
            "--workspace",
            str(root),
        ],
        cwd=str(root),
        env={
            key: os.environ[key]
            for key in (
                "PLAYWRIGHT_BROWSERS_PATH",
                "REFERENCE_BROWSER_EXECUTABLE",
                "REFERENCE_BROWSER_DEPS",
                "REFERENCE_NODE",
                "REFERENCE_FFMPEG",
            )
            if key in os.environ
        },
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        async def call(name, **args):
            result = await session.call_tool(name, args)
            if result.isError:
                raise RuntimeError(f"{name}: {result.content[0].text}")
            return result.structuredContent or json.loads(result.content[0].text)

        still, backing = [
            (await call("register_original", path=path))["id"]
            for path in ("still.png", "backing.png")
        ]
        split = await call("split_image", id=still)
        cut = await call(
            "register_image_split_mask", id=split["id"], mask_path="signal-mask.png"
        )
        # A foreground drawn below the moving signal would disappear behind it.
        # Pull it from the same still and give it its own paint order.
        occluder = await call(
            "register_image_split_mask", id=split["id"], mask_path="foreground-mask.png"
        )
        tree = await call(
            "split_image_region",
            id=split["id"],
            cuts=[
                {"key": "Signal", "selection": cut["id"]},
                {"key": "Foreground", "selection": occluder["id"]},
            ],
        )
        signal, rail = (item["selection"] for item in tree["children"][:2])
        scene = await call(
            "prepare_cutout_scene",
            id=still,
            backing=backing,
            duration=2.5,
            layers=[
                {
                    "key": "Signal",
                    "selection": signal,
                    "pivot": [252, 149],
                    "motion": [
                        {"time": 0},
                        {"time": 1.25, "move": [115, 35], "rotate": 60},
                        {"time": 2.5, "move": [245, 0], "rotate": 120},
                    ],
                },
                {"key": "ForegroundCut", "selection": rail},
            ],
        )
        print(f"Scene: {root / scene['files']['scene.json']}")
        for time, path in ((0, "rest.png"), (1.25, "crossing.png"), (2.4, "clear.png")):
            await call("render_cutout_scene", id=scene["id"], output=path, time=time)
            print(root / path)
        if video:
            job = await call(
                "render_cutout_scene", id=scene["id"], output="paper-signal.mp4"
            )
            async with asyncio.timeout(120):
                while True:
                    state = await call("inspect_cutout_video", id=job["id"])
                    if state["status"] == "completed":
                        print(root / state["render"]["path"])
                        break
                    if state["status"] not in {"queued", "running"}:
                        raise RuntimeError(state.get("error") or str(state))
                    await asyncio.sleep(0.25)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output", type=Path, help="a new directory for the sources and rendered scene"
    )
    parser.add_argument(
        "--video", action="store_true", help="also export an MP4 (FFmpeg required)"
    )
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    draw_sources(root)
    asyncio.run(make_scene(root, args.video))
