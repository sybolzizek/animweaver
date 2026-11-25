"""Full-image ownership, overlapping proposals, and lossless raster recomposition."""

import hashlib
import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageDraw

from edge_matte import refine_rgba


@dataclass
class Region:
    box: tuple[int, int, int, int]
    mask: np.ndarray
    area: int

    @classmethod
    def from_mask(cls, mask):
        ys, xs = np.where(mask)
        if not len(xs):
            return None
        left, top, right, bottom = map(
            int, (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
        )
        return cls(
            (left, top, right, bottom), mask[top:bottom, left:right].copy(), len(xs)
        )

    def within(self, values):
        left, top, right, bottom = self.box
        return values[top:bottom, left:right]

    def contains(self, point):
        x, y = point
        left, top, right, bottom = self.box
        return left <= x < right and top <= y < bottom and self.mask[y - top, x - left]

    def overlap(self, other):
        left, top, right, bottom = self.box
        ol, ot, other_right, other_bottom = other.box
        x, y = max(left, ol), max(top, ot)
        u, v = min(right, other_right), min(bottom, other_bottom)
        if x >= u or y >= v:
            return 0
        return int(
            np.count_nonzero(
                self.mask[y - top : v - top, x - left : u - left]
                & other.mask[y - ot : v - ot, x - ol : u - ol]
            )
        )


def source_rgba(work, record):
    source = record["files"].get("image.png")
    if source is None:
        raise ValueError("Select a still image, decoded frame or crop")
    return work.image(work.path(source))


def _rgba_part(rgba, region):
    data = region.within(rgba).copy()
    data[~region.mask] = 0
    return Image.fromarray(data)


def _color(i):
    # High-contrast, deterministic identifiers, not semantic colors.
    hue = (i * 103) % 180
    return cv2.cvtColor(np.uint8([[[hue, 189, 238]]]), cv2.COLOR_HSV2RGB)[0, 0]


def overlay(rgba, labels, parts):
    rgb = Image.new("RGBA", (rgba.shape[1], rgba.shape[0]), (235, 235, 235, 255))
    rgb.alpha_composite(Image.fromarray(rgba))
    rgb = np.array(rgb)[..., :3].copy()
    colors = np.stack(
        [np.zeros(3, np.uint8), *[_color(i) for i in range(1, len(parts) + 1)]]
    )
    tinted = (rgb.astype(np.float32) * 0.57 + colors[labels] * 0.43).astype(np.uint8)
    edge = np.zeros_like(labels, dtype=bool)
    edge[:-1] |= labels[:-1] != labels[1:]
    edge[:, :-1] |= labels[:, :-1] != labels[:, 1:]
    tinted[edge] = [246, 246, 246]
    image = Image.fromarray(tinted)
    scale = min(1, 1300 / max(image.size))
    image.thumbnail((1300, 1300))
    draw = ImageDraw.Draw(image)
    for part in parts:
        if part["pixels"] < rgba.shape[0] * rgba.shape[1] * 0.003:
            continue
        x, y = (round(p * scale) for p in part["point"])
        name = part["id"]
        tl, tr, br, bb = draw.textbbox((x, y), name, anchor="mm")
        draw.rectangle((tl - 4, tr - 3, br + 4, bb + 2), fill=(22, 26, 30))
        draw.text((x, y), name, anchor="mm", fill=(255, 255, 255))
    return image


def _anchor(region):
    padded = np.pad(region.mask.astype(np.uint8), 1)
    dist = cv2.distanceTransform(padded, cv2.DIST_L2, cv2.DIST_MASK_3)[1:-1, 1:-1]
    y, x = np.unravel_index(dist.argmax(), dist.shape)
    return [region.box[0] + int(x), region.box[1] + int(y)]


def _neighbors(labels):
    base = int(labels.max()) + 1
    entries = []
    for a, b in ((labels[:-1], labels[1:]), (labels[:, :-1], labels[:, 1:])):
        mask = a != b
        min_label = np.minimum(a[mask], b[mask]).astype(np.int64)
        max_label = np.maximum(a[mask], b[mask]).astype(np.int64)
        entries.append(min_label * base + max_label)
    keys, counts = np.unique(np.concatenate(entries), return_counts=True)
    return [
        {
            "a": f"R{int(k // base):03d}",
            "b": f"R{int(k % base):03d}",
            "shared_edges": int(n),
        }
        for k, n in zip(keys, counts)
    ]


def _remainder_regions(rgba, unowned):
    """Partition unclaimed pixels; no residual pixels are assigned to an invented object."""
    if not unowned.any():
        return []
    w, h = rgba.shape[1], rgba.shape[0]
    # Edges are estimated on a reduced working image. Assignment and export still use original pixels.
    ratio = min(1, 1000 / max(w, h))
    sw, sh = max(1, round(w * ratio)), max(1, round(h * ratio))
    image = Image.new("RGBA", (w, h), (128, 128, 128, 255))
    image.alpha_composite(Image.fromarray(rgba))
    reduced = cv2.resize(
        np.array(image)[..., :3], (sw, sh), interpolation=cv2.INTER_AREA
    )
    lab = cv2.cvtColor(reduced, cv2.COLOR_RGB2LAB)
    edges = np.maximum.reduce(
        [cv2.Canny(cv2.GaussianBlur(c, (3, 3), 0.6), 32, 76) for c in cv2.split(lab)]
    )
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    block = cv2.resize(edges, (w, h), interpolation=cv2.INTER_NEAREST) > 0
    # Partition the residual by surviving barriers. Assign each edge pixel to the closest seed.
    n, components, stats, _ = cv2.connectedComponentsWithStats(
        (unowned & ~block).astype(np.uint8), connectivity=8
    )
    minimum = max(32, int(w * h * 0.001))
    core = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] >= minimum) + 1
    if not len(core):
        return [(Region.from_mask(unowned), "residual_fragments")]
    seed = np.isin(components, core)
    _, nearest = cv2.distanceTransformWithLabels(
        (~seed).astype(np.uint8),
        cv2.DIST_L2,
        cv2.DIST_MASK_5,
        labelType=cv2.DIST_LABEL_PIXEL,
    )
    remap = np.zeros(int(nearest.max()) + 1, np.int32)
    remap[nearest[seed]] = components[seed]
    nearest_component = remap[nearest]
    nearest_component[~unowned] = 0
    # A nearest-seed cell can contain fragments separated by an accepted model mask.
    # Do not put unrelated islands into its editable crop. Gather small scraps as explicit residue.
    scraps = unowned.copy()
    output = []
    for key in core:
        cell = Region.from_mask(nearest_component == key)
        if cell is None:
            continue
        _, islands, island_stats, _ = cv2.connectedComponentsWithStats(
            cell.mask.astype(np.uint8), connectivity=8
        )
        for label in np.flatnonzero(island_stats[1:, cv2.CC_STAT_AREA] >= minimum) + 1:
            x, y, width, height, count = map(int, island_stats[label])
            left, top, _, _ = cell.box
            mask = islands[y : y + height, x : x + width] == label
            region = Region(
                (left + x, top + y, left + x + width, top + y + height), mask, count
            )
            region.within(scraps)[mask] = False
            output.append((region, "residual"))
    remainder = Region.from_mask(scraps)
    if remainder is not None:
        output.append((remainder, "residual_fragments"))
    if sum(r.area for r, _ in output) != int(np.count_nonzero(unowned)):
        raise ValueError("Residual partition did not cover the image")
    return output


def build(work, source):
    rgba = source_rgba(work, source)
    pixels = np.asarray(rgba)
    w, h = rgba.size
    files = {}

    labels = np.zeros((h, w), dtype=np.uint16)
    parts = []

    def put(region, kind):
        i = len(parts) + 1
        if i > 65535:
            raise ValueError("Too many separate regions for the label map")
        name = f"R{i:03d}"
        region.within(labels)[region.mask] = i
        name_png = f"part-{name}.png"
        part = _rgba_part(pixels, region)
        files[name_png] = part
        parts.append(
            dict(
                id=name,
                label=i,
                kind=kind,
                box=list(region.box),
                pixels=region.area,
                point=_anchor(region),
                image=name_png,
                color=_color(i).tolist(),
            )
        )

    for region, kind in _remainder_regions(pixels, labels == 0):
        put(region, kind)
    if np.any(labels == 0) or sum(p["pixels"] for p in parts) != w * h:
        raise ValueError("Image has unassigned pixels")
    files["partition.png"] = Image.fromarray(labels)
    files["preview.png"] = overlay(pixels, labels, parts)
    return work.create(
        dict(
            kind="image_split",
            origin="derived",
            parent=source["id"],
            width=w,
            height=h,
            inference={"backend": "edges-only"},
            parts=parts,
            neighbors=_neighbors(labels),
            partition_sha256=hashlib.sha256(labels.tobytes()).hexdigest(),
        ),
        files,
    )


def _labels(work, record):
    if record["kind"] != "image_split":
        raise ValueError("Select an image split")
    with Image.open(work.path(record["files"]["partition.png"])) as file:
        data = np.asarray(file, dtype=np.uint16)
    if (
        data.shape != (record["height"], record["width"])
        or hashlib.sha256(data.tobytes()).hexdigest() != record["partition_sha256"]
    ):
        raise ValueError("The label map does not match the split")
    return data


def group(work, record, refs, include, exclude):
    labels = _labels(work, record)
    w, h = record["width"], record["height"]
    if set(include) & set(exclude) or any(
        not (0 <= x < w and 0 <= y < h) for x, y in [*include, *exclude]
    ):
        raise ValueError("Selection points must fit the image and cannot conflict")
    if not refs and not include:
        raise ValueError("Select one or more region IDs, or give foreground points")
    parts = {row["id"]: row for row in record["parts"]}
    proposals = {row["id"]: row for row in record.get("proposals", [])}
    original = source_rgba(work, work.load(record["parent"]))
    pixels = np.asarray(original)

    def get(ref):
        if ref not in parts and ref not in proposals:
            raise ValueError(f"Unknown split region: {ref}")
        row = parts[ref] if ref in parts else proposals[ref]
        left, top, right, bottom = row["box"]
        if ref in parts:
            mask = labels[top:bottom, left:right] == row["label"]
        else:
            with Image.open(work.path(record["files"][row["mask"]])) as img:
                mask = np.array(img) > 0
        return Region(tuple(row["box"]), mask, int(np.count_nonzero(mask)))

    def fits(region):
        return all(region.contains(p) for p in include) and not any(
            region.contains(p) for p in exclude
        )

    method = "stored"
    if refs:
        full = np.zeros((h, w), bool)
        for ref in refs:
            region = get(ref)
            region.within(full)[:] |= region.mask
        result = Region.from_mask(full)
        if not fits(result):
            raise ValueError("Selected regions do not match the include/exclude points")
    else:
        refs = []
        result = None
        # Prefer the smallest existing proposal that satisfies the selection. No re-encoding unless needed.
        proposals_to_check = sorted(
            proposals.values(), key=lambda row: (row["pixels"], -row["score"])
        )
        for row in proposals_to_check:
            left, top, right, bottom = row["box"]
            if not all(left <= x < right and top <= y < bottom for x, y in include):
                continue
            candidate = get(row["id"])
            if fits(candidate):
                result = candidate
                refs = [row["id"]]
                break
        if result is None:
            candidates = [int(labels[y, x]) for x, y in include]
            candidate = Region.from_mask(np.isin(labels, candidates))
            if not fits(candidate):
                raise ValueError(
                    "No selection matches those points; choose region IDs or another point"
                )
            result = candidate
            method = "partition"
            refs = [f"R{i:03d}" for i in sorted(set(candidates))]
    left, top, right, bottom = result.box
    return work.create(
        dict(
            kind="image_split_selection",
            origin="derived",
            parent=record["id"],
            width=right - left,
            height=bottom - top,
            source_box=list(result.box),
            pixels=result.area,
            refs=refs,
            include=[list(p) for p in include],
            exclude=[list(p) for p in exclude],
            method=method,
            mask_sha256=hashlib.sha256(result.mask.tobytes()).hexdigest(),
        ),
        {
            "image.png": _rgba_part(pixels, result),
            "mask.png": Image.fromarray(result.mask.astype(np.uint8) * 255),
        },
    )


def import_mask(work, basis, mask_path, box, threshold):
    selection = basis if basis["kind"] == "image_split_selection" else None
    split = work.load(selection["parent"]) if selection else basis
    if split["kind"] != "image_split":
        raise ValueError("Choose an image split or a hard selection from it")
    width, height = split["width"], split["height"]
    x, y, r, b = box if box is not None else (0, 0, width, height)
    if not (0 <= x < r <= width and 0 <= y < b <= height):
        raise ValueError("Mask box must fit the split's source image")
    path = work.path(mask_path)
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as mask:
        if mask.format != "PNG" or mask.size != (r - x, b - y):
            raise ValueError(
                "Use a PNG with dimensions matching the source or the specified box"
            )
        if "A" in mask.getbands():
            channel = mask.getchannel("A")
        elif mask.mode in ("1", "L"):
            channel = mask.convert("L")
        else:
            raise ValueError(
                "Use an 8-bit grayscale PNG or a PNG with an alpha channel"
            )
        region = Region.from_mask(np.asarray(channel) >= threshold)
    if region is None:
        raise ValueError("Mask selects no pixels at this threshold")
    left, top, right, bottom = region.box
    region.box = (x + left, y + top, x + right, y + bottom)
    source = source_rgba(work, work.load(split["parent"]))
    return work.create(
        dict(
            kind="image_split_selection",
            origin="derived",
            parent=split["id"],
            method="imported_mask",
            width=right - left,
            height=bottom - top,
            source_box=list(region.box),
            pixels=region.area,
            mask_sha256=hashlib.sha256(region.mask.tobytes()).hexdigest(),
            inputs={
                "mask_path": work.rel(path),
                "mask_file_sha256": hashlib.sha256(raw).hexdigest(),
                "input_box": [x, y, r, b],
                "threshold": threshold,
                **({"previous_selection": selection["id"]} if selection else {}),
            },
        ),
        {
            "image.png": _rgba_part(np.asarray(source), region),
            "mask.png": Image.fromarray(region.mask.astype(np.uint8) * 255),
        },
    )


def selection_mask(work, selection):
    with Image.open(work.path(selection["files"]["mask.png"])) as file:
        mask = np.array(file.convert("L")) > 0
    if hashlib.sha256(mask.tobytes()).hexdigest() != selection["mask_sha256"]:
        raise ValueError("Selection mask does not match its record")
    return mask


def refine_edges(work, selection, band_px):
    if selection["kind"] != "image_split_selection":
        raise ValueError("Select an image split crop first")
    split = work.load(selection["parent"])
    original = source_rgba(work, work.load(split["parent"]))
    left, top, right, bottom = selection["source_box"]

    def expanded(padding):
        return (
            max(0, left - padding),
            max(0, top - padding),
            min(original.width, right + padding),
            min(original.height, bottom + padding),
        )

    # Retain context outside the export box so the solver has actual background
    # pixels even at the edge of a tightly cropped selection.
    cx, cy, cr, cb = context = expanded(3 * band_px + 1)
    stored = selection_mask(work, selection)
    mask = np.zeros((cb - cy, cr - cx), dtype=bool)
    mask[top - cy : bottom - cy, left - cx : right - cx] = stored
    rgba = np.asarray(original.crop(context))
    refined = refine_rgba(rgba, mask, band_px)
    x, y, r, b = box = expanded(band_px)
    frame = np.s_[y - cy : b - cy, x - cx : r - cx]
    result = refined[frame]
    alpha = result[..., 3]
    hard_alpha = np.where(mask[frame], rgba[frame][..., 3], 0)
    return work.create(
        dict(
            kind="image_split_matte",
            origin="derived",
            parent=selection["id"],
            method="local_color_line_v1",
            band_px=band_px,
            source_box=list(box),
            width=r - x,
            height=b - y,
            changed_alpha_pixels=int(np.count_nonzero(alpha != hard_alpha)),
            fractional_alpha_pixels=int(np.count_nonzero((alpha > 0) & (alpha < 255))),
            pixel_sha256=hashlib.sha256(result.tobytes()).hexdigest(),
        ),
        {"image.png": Image.fromarray(result), "alpha.png": Image.fromarray(alpha)},
    )


def recompose(work, record):
    labels = _labels(work, record)
    result = np.zeros((*labels.shape, 4), np.uint8)
    written = 0
    for row in record["parts"]:
        left, top, right, bottom = row["box"]
        mask = labels[top:bottom, left:right] == row["label"]
        with Image.open(work.path(record["files"][row["image"]])) as img:
            rgba = np.array(img.convert("RGBA"))
        if (
            rgba.shape != (bottom - top, right - left, 4)
            or int(mask.sum()) != row["pixels"]
        ):
            raise ValueError("Part does not match the split")
        result[top:bottom, left:right][mask] = rgba[mask]
        written += int(mask.sum())
    if written != labels.size:
        raise ValueError("Incomplete recomposition")
    # Only now open the original to report a comparison; no source pixels participate in assembly.
    source = np.asarray(source_rgba(work, work.load(record["parent"])))
    difference = int(np.count_nonzero(np.any(result != source, axis=-1)))
    return work.create(
        dict(
            kind="image_recomposition",
            origin="derived",
            parent=record["id"],
            width=record["width"],
            height=record["height"],
            changed_pixels=difference,
            pixel_sha256=hashlib.sha256(result.tobytes()).hexdigest(),
        ),
        {"image.png": Image.fromarray(result)},
    )
