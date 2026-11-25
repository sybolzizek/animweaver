"""Local image materials and cutout scenes over MCP stdio."""

import argparse
import hashlib
import io
import json
import math
import os
import re
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage, ImageDraw, ImageOps

from comparison import Alignment, PointPair, compare
from image_tools import register_image_tools
from scene_export import run_job

MATERIAL_ID = re.compile(r"[0-9a-f]{24}")


class Materials:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.store = self.path(".reference-materials")
        self.store.mkdir(exist_ok=True)

    def path(self, name):
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Path escapes the workspace")
        return path

    def rel(self, path):
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def load(self, ref_id):
        if not MATERIAL_ID.fullmatch(ref_id):
            raise ValueError("Invalid material ID")
        return json.loads(
            self.path(f".reference-materials/{ref_id}/record.json").read_text("utf-8")
        )

    @staticmethod
    def put(path, data):
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            tmp = Path(stream.name)
            try:
                stream.write(data)
            except BaseException:
                stream.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def create(self, record, files=None):
        key = json.dumps(
            record, sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()
        ref_id = hashlib.sha256(key).hexdigest()[:24]
        directory = self.path(f".reference-materials/{ref_id}")
        directory.mkdir(exist_ok=True)
        record = {"id": ref_id, **record, "files": {}}
        for name, image in (files or {}).items():
            dest = self.path(f".reference-materials/{ref_id}/{name}")
            if not isinstance(image, bytes):
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                image = stream.getvalue()
            self.put(dest, image)
            record["files"][name] = self.rel(dest)
        self.put(
            directory / "record.json",
            json.dumps(record, ensure_ascii=False, indent=2).encode(),
        )
        return record

    @staticmethod
    def image(path):
        with PILImage.open(path) as image:
            return ImageOps.exif_transpose(image).convert("RGBA")

    def register(
        self, name, origin="original", request=None, provider=None, capture_note=None
    ):
        path = self.path(name)
        if not path.is_file() or path.is_relative_to(self.store):
            raise ValueError("Select an input image outside the material store")
        checksum = self.digest(path)
        try:
            image = self.image(path)
        except (OSError, SyntaxError) as exc:
            raise ValueError("Expected a readable still image") from exc
        if self.digest(path) != checksum:
            raise ValueError("Image changed during registration")
        record = dict(
            kind="source",
            origin=origin,
            path=self.rel(path),
            sha256=checksum,
            media="image",
            width=image.width,
            height=image.height,
        )
        if request is not None:
            record.update(parent=request, provider=provider)
        if capture_note:
            record["capture_note"] = capture_note
        return self.create(record, {"image.png": image})

    @staticmethod
    def box(box, w, h):
        if len(box) != 4:
            raise ValueError("Box is [left, top, right, bottom]")
        left, top, right, bottom = box
        if not (0 <= left < right <= w and 0 <= top < bottom <= h):
            raise ValueError("Box must fit within the image")
        return left, top, right, bottom

    def region(self, source, box, polygon):
        image = self.image(self.path(source["files"]["image.png"]))
        left, top, right, bottom = self.box(box, *image.size)
        local = image.crop((left, top, right, bottom))
        files = {"image.png": local}
        if polygon is not None:
            if len(polygon) < 3 or any(
                not (left <= x < right and top <= y < bottom) for x, y in polygon
            ):
                raise ValueError("Polygon needs three or more points inside the box")
            mask = PILImage.new("L", local.size)
            ImageDraw.Draw(mask).polygon(
                [(x - left, y - top) for x, y in polygon], fill=255
            )
            from PIL import ImageChops

            local.putalpha(ImageChops.darker(local.getchannel("A"), mask))
            files["mask.png"] = mask
        return self.create(
            dict(
                kind="region",
                origin="derived",
                parent=source["id"],
                box=box,
                polygon=polygon,
                width=local.width,
                height=local.height,
            ),
            files,
        )


def serve(root):
    work = Materials(root)
    mcp = FastMCP(
        "reference-materials",
        instructions=(
            "Local images, editable cutout trees, and renderable scenes. Before rigging a "
            "shot, identify what shares a surface, what must stay in contact and what must "
            "pass in front or behind. Split only where motion or occlusion requires it. "
            "Inspect a frame where those relationships are most stressed before final export. "
            "Read reference-materials://guide/compositing for coordinate conventions and repair choices."
        ),
    )

    @mcp.resource("reference-materials://guide/compositing", mime_type="text/markdown")
    def compositing_guide() -> str:
        return Path(__file__).with_name("AGENT-GUIDE.md").read_text(encoding="utf-8")

    @mcp.tool()
    def register_original(path: str) -> dict:
        """Snapshot an image from the workspace and return the material ID and original dimensions."""
        return work.register(path)

    @mcp.tool()
    def register_render(path: str, capture_note: str | None = None) -> dict:
        """Register a rendered image for inspection, comparison or use in another scene."""
        return work.register(path, origin="rendered", capture_note=capture_note)

    @mcp.tool()
    def inspect_material(id: str) -> dict:
        """Read an asset's record and its known input materials; paths are relative to the workspace."""
        record = work.load(id)

        def follow(item, include_start=False):
            lineage, seen = [], set()
            if not include_start:
                item = work.load(item["parent"]) if item.get("parent") else None
            while item and item["id"] not in seen:
                seen.add(item["id"])
                lineage.append({k: item[k] for k in ("id", "kind", "origin")})
                item = work.load(item["parent"]) if item.get("parent") else None
            return lineage

        upstream = {
            role: follow(work.load(ref), True)
            for role, ref in record.get("inputs", {}).items()
            if isinstance(ref, str) and MATERIAL_ID.fullmatch(ref)
        }
        return {
            **record,
            "lineage": follow(record),
            **({"input_lineage": upstream} if upstream else {}),
        }

    @mcp.tool()
    def extract_region(
        id: str,
        box: tuple[int, int, int, int],
        polygon: list[tuple[int, int]] | None = None,
    ) -> dict:
        """Crop an image in its own pixel coordinates, optionally with a polygon mask."""
        return work.region(work.load(id), box, polygon)

    @mcp.tool()
    def measure_path(id: str, points: list[tuple[int, int]]) -> dict:
        """Measure a polyline in image pixels, not world-space distances."""
        record = work.load(id)
        w, h = record["width"], record["height"]
        if (
            "image.png" not in record["files"]
            or len(points) < 2
            or any(not (0 <= x < w and 0 <= y < h) for x, y in points)
        ):
            raise ValueError("Need an image and at least two points inside it")
        lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
        return {
            "id": id,
            "segments_px": lengths,
            "total_px": sum(lengths),
            "normalized": [[x / w, y / h] for x, y in points],
        }

    @mcp.tool()
    def compare_images(
        reference_id: str,
        candidate_id: str,
        alignment: Alignment = "identity",
        anchors: list[PointPair] | None = None,
        checks: list[PointPair] | None = None,
        focus_box: tuple[int, int, int, int] | None = None,
        reference_polygon: list[tuple[float, float]] | None = None,
        candidate_polygon: list[tuple[float, float]] | None = None,
    ) -> dict:
        """Compare source and rendered images; anchors align framing and checks measure independent points."""
        keys = {"reference": reference_id, "candidate": candidate_id}
        images = [
            work.image(work.path(work.load(mid)["files"]["image.png"]))
            for mid in keys.values()
        ]
        focus = work.box(focus_box or (0, 0, *images[0].size), *images[0].size)
        result, files = compare(
            *images,
            anchors or [],
            checks or [],
            alignment,
            focus,
            reference_polygon,
            candidate_polygon,
        )
        return work.create(
            dict(
                kind="comparison",
                origin="derived",
                inputs=keys,
                alignment=alignment,
                focus_box=list(focus),
                reference_polygon=reference_polygon,
                candidate_polygon=candidate_polygon,
                width=focus[2] - focus[0],
                height=focus[3] - focus[1],
                **result,
            ),
            files,
        )

    @mcp.tool()
    def request_supplement(
        id: str, instruction: str, expected_media: str = "image"
    ) -> dict:
        """Record a request for an external image supplement; does not call a generator."""
        parent = work.load(id)
        if (
            parent["kind"] == "supplement-request"
            or expected_media != "image"
            or not instruction.strip()
        ):
            raise ValueError("Supply an image supplement instruction and a material ID")
        return work.create(
            dict(
                kind="supplement-request",
                origin="request",
                parent=id,
                expected_media="image",
                instruction=instruction.strip(),
            )
        )

    @mcp.tool()
    def register_supplement(request_id: str, path: str, provider: str) -> dict:
        """Register the real image received from an external tool, with provenance."""
        request = work.load(request_id)
        if (
            request["kind"] != "supplement-request"
            or request["expected_media"] != "image"
            or not provider.strip()
        ):
            raise ValueError("Expected an image supplement request and provider")
        return work.register(path, "generated", request_id, provider.strip())

    @mcp.tool()
    def preview_material(id: str, file: str = "image.png") -> Image:
        """View a PNG stored in the material record, scaled for inspection only."""
        data = work.load(id)
        if not file.endswith(".png") or file not in data["files"]:
            raise ValueError("This material has no such PNG")
        image = work.image(work.path(data["files"][file]))
        image.thumbnail((1400, 1400))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return Image(data=stream.getvalue(), format="png")

    register_image_tools(mcp, work)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--render-job", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.render_job:
        run_job(Materials(args.workspace), args.render_job)
    else:
        serve(args.workspace)
