import asyncio
import functools
import http.server
import os
import subprocess
import threading

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from test_stdio import call, connect


def test_split_masks_bind_a_continuous_mesh_in_chrome(tmp_path):
    deps = os.environ.get("REFERENCE_BROWSER_DEPS")
    chrome = os.environ.get("REFERENCE_BROWSER_EXECUTABLE")
    if not deps or not chrome:
        pytest.skip("Set the browser dependency and executable paths")
    source = Image.new("RGB", (160, 112), "#141414")
    draw = ImageDraw.Draw(source)
    draw.rectangle((30, 44, 113, 56), fill="#ff3030")
    draw.rectangle((114, 44, 124, 56), fill="#30ee30")
    draw.rectangle((60, 84, 71, 96), fill="#233cce")
    draw.rectangle((90, 84, 101, 96), fill="#e1c030")
    source.save(tmp_path / "image.png")

    def box(key, left, right, top=44, bottom=56):
        return {
            "key": key,
            "polygon": [[left, top], [right, top], [right, bottom], [left, bottom]],
        }

    async def scene():
        async with connect(tmp_path) as session:
            orig = await call(session, "register_original", path="image.png")
            split = await call(session, "split_image", id=orig["id"])
            tree = await call(
                session,
                "split_image_region",
                id=split["id"],
                cuts=[
                    box("sleeve", 30, 113),
                    box("hand", 114, 124),
                    box("blue", 60, 71, 84, 96),
                    box("yellow", 90, 101, 84, 96),
                ],
            )
            sleeve, hand, blue, yellow = [
                row["selection"] for row in tree["children"][:4]
            ]
            branch = await call(
                session,
                "split_image_region",
                id=tree["id"],
                parent="root/sleeve",
                cuts=[box("upper", 30, 74), box("lower", 75, 113)],
            )
            upper, lower = [row["selection"] for row in branch["children"]]
            groups = [
                {"key": "Upper", "pivot": [30, 50]},
                {
                    "key": "Lower",
                    "parent": "Upper",
                    "pivot": [75, 50],
                    "motion": [{"time": 0}, {"time": 1, "rotate": 30}],
                },
            ]
            layers = [
                {
                    "key": "Sleeve",
                    "selection": sleeve,
                    "mesh": {
                        "bones": [
                            {"key": "Upper", "selection": upper},
                            {"key": "Lower", "selection": lower},
                        ],
                        "blend": 12,
                        "cell": 4,
                    },
                },
                {"key": "Hand", "selection": hand, "parent": "Lower"},
                {"key": "Blue", "selection": blue},
                {
                    "key": "Yellow",
                    "selection": yellow,
                    "motion": [{"time": 0}, {"time": 0.4, "move": [-30, 0]}],
                },
            ]
            built = await call(
                session,
                "prepare_cutout_scene",
                id=split["id"],
                layers=layers,
                groups=groups,
            )
            assert "cutout-mesh.mjs" in built["files"]
            broken = [layers[0] | {"parent": "Lower"}, layers[1]]
            await call(
                session,
                "prepare_cutout_scene",
                id=split["id"],
                layers=broken,
                groups=groups,
                fail=True,
            )
            await call(
                session,
                "revise_cutout_scene",
                id=built["id"],
                fail=True,
                depth={"Yellow": [{"time": 0.8, "z": 0}, {"time": 0.2, "z": 1}]},
            )
            revised = await call(
                session,
                "revise_cutout_scene",
                id=built["id"],
                depth={"Yellow": [{"time": 0.75, "z": -1}]},
            )
            assert revised["claim_order"] == built["claim_order"]
            assert not built["layers"][-1]["depth"]
            return revised["files"]["scene.json"], revised["files"]["cutout-scene.mjs"]

    spec, runtime = asyncio.run(scene())
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=tmp_path
    )
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            subprocess.run(
                [
                    "node",
                    "-",
                    deps,
                    chrome,
                    f"http://127.0.0.1:{server.server_port}",
                    spec,
                    runtime,
                ],
                cwd=tmp_path,
                check=True,
                timeout=30,
                text=True,
                input=r"""
const [deps, executablePath, host, spec, runtime] = process.argv.slice(2);
const {chromium} = require(require('node:path').join(deps, 'playwright-core'));
const {writeFileSync} = require('node:fs');
(async () => {
  const browser = await chromium.launch({executablePath, args:['--enable-unsafe-swiftshader']});
  try {
    const page = await browser.newPage();
    await page.goto(`${host}/${spec}`);
    const frames = await page.evaluate(async ([spec,runtime]) => {
      const {loadCutoutScene} = await import(`/${runtime}`);
      const canvas = document.createElement('canvas');
      const scene = await loadCutoutScene(canvas, `/${spec}`);
      return [0,.5,.75,1,.5].map(t => { scene.seek(t); return canvas.toDataURL(); });
    }, [spec,runtime]);
    frames.forEach((frame,i) => writeFileSync(`${i}.png`, Buffer.from(frame.split(',')[1], 'base64')));
  } finally { await browser.close(); }
})().catch(err => {console.error(err); process.exitCode = 1;});
""",
            )
        finally:
            server.shutdown()

    with Image.open(tmp_path / "0.png") as rest, Image.open(tmp_path / "3.png") as bent:
        assert rest.convert("RGB").tobytes() == source.tobytes()
        frame = np.asarray(bent)
        red = ((frame[:, :, 0] > 200) & (frame[:, :, 1] < 90)).astype(np.uint8)
        count, labels = cv2.connectedComponents(red)
        assert count == 2 and labels[50, 38] == labels[50, 75] == labels[65, 101] == 1
        assert frame[72, 113, 1] > 200
        assert frame[50, 119, 1] < 90
    for i, color in [(1, (225, 192, 48)), (2, (35, 60, 206)), (4, (225, 192, 48))]:
        with Image.open(tmp_path / f"{i}.png") as frame:
            assert frame.getpixel((65, 90))[:3] == color
