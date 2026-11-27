"""Assemble editable scene layers from one or more image materials."""

import json
import math
from pathlib import Path
from typing import Annotated, Literal

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from image_split import Region, _rgba_part, selection_mask, source_rgba
from scene_appearance import Appearance, ColorGrade, apply_grade
from scene_planner import plan_scene
from scene_seams import Seam, build as build_seam


Key = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Positive = Annotated[FiniteFloat, Field(gt=0)]
NonNegative = Annotated[FiniteFloat, Field(ge=0)]
Point = tuple[FiniteFloat, FiniteFloat]
Ease = Literal["linear", "smooth", "ease_in", "ease_out", "hold"]
Blend = Literal[
    "normal",
    "add",
    "multiply",
    "screen",
    "overlay",
    "darken",
    "lighten",
    "color-dodge",
    "color-burn",
    "hard-light",
    "soft-light",
    "difference",
    "exclusion",
    "hue",
    "saturation",
    "color",
    "luminosity",
]
TimeMode = Literal["free", "clamp", "repeat", "pingpong"]


class SurfaceMount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Key
    point: Point
    towards: Point

    @model_validator(mode="after")
    def axis(self):
        if math.dist(self.point, self.towards) < 1:
            raise ValueError("A surface mount needs two distinct points")
        return self


class Underlap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    at: tuple[int, int]
    grade: ColorGrade | None = None


class LocalMatte(BaseModel):
    """A moving alpha stencil applied to one layer, not to the complete scene."""

    model_config = ConfigDict(extra="forbid")
    target: Key
    mask: Key
    operation: Literal["keep", "erase"] = "erase"
    windows: list[tuple[FiniteFloat, FiniteFloat]] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self):
        if self.target == self.mask:
            raise ValueError("A matte must use a different cutout")
        last = 0
        for start, end in self.windows:
            if start < last or end <= start:
                raise ValueError(
                    "Matte windows must be ordered, nonoverlapping and have duration"
                )
            last = end
        return self


class SoftShadow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caster: Key
    receiver: Key | None = None
    origin: Point | None = None
    offset: Point = (0, 4)
    scale: tuple[Positive, Positive] = (1, 1)
    blur: Annotated[FiniteFloat, Field(ge=0, le=128)] = 4
    opacity: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.25
    color: tuple[
        Annotated[int, Field(ge=0, le=255)],
        Annotated[int, Field(ge=0, le=255)],
        Annotated[int, Field(ge=0, le=255)],
    ] = (0, 0, 0)


class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: Annotated[FiniteFloat, Field(ge=0)]
    move: tuple[FiniteFloat, FiniteFloat] = (0, 0)
    rotate: FiniteFloat = 0
    scale: tuple[Positive, Positive] = (1, 1)
    ease: Ease = "smooth"


class OpacityKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: Annotated[FiniteFloat, Field(ge=0)]
    value: Annotated[FiniteFloat, Field(ge=0, le=1)]
    ease: Ease = "smooth"


class CameraPose(BaseModel):
    """A framing key. Ease controls the interval beginning at this key."""

    model_config = ConfigDict(extra="forbid")
    time: Annotated[FiniteFloat, Field(ge=0)]
    move: Point = (0, 0)
    truck: Point = (0, 0)
    dolly: FiniteFloat = 0
    zoom: Positive = 1
    rotate: FiniteFloat = 0
    ease: Ease = "smooth"


class CameraShake(BaseModel):
    """A deterministic screen-space impact cue layered over the camera track."""

    model_config = ConfigDict(extra="forbid")
    time: Annotated[FiniteFloat, Field(ge=0)]
    duration: Positive
    amplitude: tuple[NonNegative, NonNegative] = (0, 0)
    rotate: Annotated[FiniteFloat, Field(ge=0)] = 0
    frequency: Annotated[FiniteFloat, Field(gt=0, le=120)] = 18
    decay: Annotated[FiniteFloat, Field(ge=0, le=8)] = 2
    seed: Annotated[int, Field(ge=-1_000_000, le=1_000_000)] = 0


class CameraTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pivot: Point | None = None
    focal: Positive | None = None
    motion: list[CameraPose] = Field(default_factory=list)
    shakes: list[CameraShake] = Field(default_factory=list)


class LocalTime(BaseModel):
    """Map a parent clock into the clock used by one node and its descendants."""

    model_config = ConfigDict(extra="forbid")
    start: FiniteFloat = 0
    offset: FiniteFloat = 0
    scale: FiniteFloat = 1
    duration: Positive | None = None
    mode: TimeMode = "free"

    @model_validator(mode="after")
    def bounded_mode(self):
        if self.mode == "free" and self.duration is not None:
            raise ValueError("Free local time must not declare a duration")
        if self.mode != "free" and self.duration is None:
            raise ValueError("Clamp, repeat and pingpong local time need a duration")
        return self


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Key
    parent: Key | None = None
    pivot: tuple[FiniteFloat, FiniteFloat] = (0, 0)
    motion: list[Pose] = Field(default_factory=list)
    mount: SurfaceMount | None = None
    plane_depth: FiniteFloat = 0
    opacity: list[OpacityKey] = Field(default_factory=list)
    blend_mode: Blend = "normal"
    local_time: LocalTime | None = None


class BoneBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Key
    tip: tuple[FiniteFloat, FiniteFloat] | None = None
    selection: str | None = None


class Skin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bones: Annotated[list[BoneBinding], Field(min_length=1)]
    blend: Positive = 28
    cell: Annotated[int, Field(ge=2, le=128)] = 10
    domain: tuple[int, int, int, int] | None = None


class DepthKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: Annotated[FiniteFloat, Field(ge=0)]
    z: FiniteFloat


class Cutout(Node):
    selection: str | None = None
    source: str | None = None
    scene: str | None = None
    edge_matte: str | None = None
    offset: tuple[int, int] = (0, 0)
    mesh: Skin | None = None
    depth: list[DepthKey] = Field(default_factory=list)
    underlap: list[Underlap] = Field(default_factory=list)
    paintover: list[Underlap] = Field(default_factory=list)
    matte_only: bool = False
    appearance: Appearance | None = None

    @model_validator(mode="after")
    def one_source(self):
        if sum(value is not None for value in (self.selection, self.source, self.scene)) != 1:
            raise ValueError(
                "Provide exactly one hard selection, image source or scene source for each layer"
            )
        if self.scene is not None and (
            self.edge_matte is not None
            or self.mesh is not None
            or self.underlap
            or self.paintover
            or self.matte_only
            or self.appearance is not None
        ):
            raise ValueError(
                "A scene source cannot use raster edge, mesh, paint, matte-only or appearance fields"
            )
        if self.matte_only and self.selection is not None:
            raise ValueError(
                "A matte-only cutout must use an image source and not claim scene pixels"
            )
        if self.edge_matte is not None and self.selection is None:
            raise ValueError("A refined edge matte needs a hard cutout selection")
        return self


def _validate_scene_dependency(work, ref_id, trail=()):
    if ref_id in trail:
        raise ValueError(f"Cyclic scene source dependency at {ref_id}")
    scene = work.load(ref_id)
    if scene.get("kind") != "cutout_scene":
        raise ValueError("A scene source must reference an editable cutout scene")
    for layer in scene.get("layers", []):
        if child := layer.get("scene"):
            _validate_scene_dependency(work, child, (*trail, ref_id))
    return scene


def _lineage(work, selection):
    if selection["kind"] != "image_split_selection":
        raise ValueError("A scene cutout must use a hard image split selection")
    return work.load(selection["parent"])["parent"]


def _bind_mesh(work, skin, region, groups, source_id, offset):
    left, top, right, bottom = region.box
    width, height = right - left, bottom - top
    cols, rows = (
        (width + skin.cell - 1) // skin.cell,
        (height + skin.cell - 1) // skin.cell,
    )
    x, y = np.meshgrid(
        np.linspace(0, width, cols + 1), np.linspace(0, height, rows + 1)
    )
    sample_x = np.clip(np.floor(x).astype(int), 0, width - 1)
    sample_y = np.clip(np.floor(y).astype(int), 0, height - 1)
    distances = []
    for bone in skin.bones:
        if bone.selection is not None:
            selection = work.load(bone.selection)
            if _lineage(work, selection) != source_id:
                raise ValueError("Mesh weight masks must use the same source image")
            a, b, c, d = selection["source_box"]
            x0, y0, x1, y1 = max(a, left), max(b, top), min(c, right), min(d, bottom)
            mask = np.zeros(region.mask.shape, dtype=bool)
            if x0 < x1 and y0 < y1:
                mask[y0 - top : y1 - top, x0 - left : x1 - left] = selection_mask(
                    work, selection
                )[y0 - b : y1 - b, x0 - a : x1 - a]
            mask &= region.mask
            if not mask.any():
                raise ValueError(
                    f"Weight mask for {bone.key} has no pixels on its mesh"
                )
            distance = cv2.distanceTransform((~mask).astype(np.uint8), cv2.DIST_L2, 5)
            distances.append(distance[sample_y, sample_x])
        else:
            px, py = groups[bone.key].pivot
            tx, ty = bone.tip if bone.tip is not None else (px, py)
            dx, dy = tx - px, ty - py
            gx, gy = x + left + offset[0] - px, y + top + offset[1] - py
            if dx or dy:
                t = np.clip((gx * dx + gy * dy) / (dx * dx + dy * dy), 0, 1)
                gx, gy = gx - dx * t, gy - dy * t
            distances.append(np.hypot(gx, gy))
    distance = np.stack(distances, axis=-1)
    t = np.clip(
        1 - (distance - distance.min(axis=-1, keepdims=True)) / skin.blend, 0, 1
    )
    weights = t * t * (3 - 2 * t)
    weights /= weights.sum(axis=-1, keepdims=True)
    return {
        "bones": [bone.key for bone in skin.bones],
        "grid": [cols, rows],
        "weights": np.round(weights.reshape(-1, len(skin.bones)), 6).tolist(),
    }


def _with_paint(layer, original, region, image_for):
    if not (layer.underlap or layer.paintover):
        return original, region, 0
    left, top, right, bottom = region.box
    patches = [
        (patch.at, apply_grade(Image.fromarray(image_for(patch.source)), patch.grade))
        for patch in layer.underlap + layer.paintover
    ]
    x0 = min(left, *(x for (x, y), patch in patches))
    y0 = min(top, *(y for (x, y), patch in patches))
    x1 = max(right, *(x + patch.width for (x, y), patch in patches))
    y1 = max(bottom, *(y + patch.height for (x, y), patch in patches))
    # Permit patches outside a cropped asset but avoid accidentally unbounded rasters.
    if (x1 - x0) * (y1 - y0) > 32_000_000:
        raise ValueError(
            f"Paint patches for {layer.key} expand the layer beyond 32 million pixels"
        )
    draw = Image.new("RGBA", (x1 - x0, y1 - y0))
    for (x, y), patch in patches[: len(layer.underlap)]:
        draw.alpha_composite(patch, (x - x0, y - y0))
    draw.alpha_composite(original, (left - x0, top - y0))
    extra = (
        int(np.count_nonzero(np.asarray(draw.getchannel("A")))) - region.area
        if layer.underlap
        else 0
    )
    for (x, y), patch in patches[len(layer.underlap) :]:
        draw.alpha_composite(patch, (x - x0, y - y0))
    alpha = draw.getchannel("A")
    a, b, c, d = alpha.getbbox()
    local = np.asarray(alpha)[b:d, a:c] > 0
    painted = (
        draw.crop((a, b, c, d)),
        Region((x0 + a, y0 + b, x0 + c, y0 + d), local, int(local.sum())),
    )
    return (*painted, extra)


def _align_skin_domain(layer, pixels, region):
    """Two materials can use the same grid coordinates without sharing a raster or draw order."""
    domain = layer.mesh.domain if layer.mesh else None
    if domain is None or domain == region.box:
        return pixels, region
    left, top, right, bottom = domain
    x0, y0, x1, y1 = region.box
    if not (left <= x0 < x1 <= right and top <= y0 < y1 <= bottom):
        raise ValueError(f"Mesh domain for {layer.key} must enclose all painted pixels")
    if (right - left) * (bottom - top) > 32_000_000:
        raise ValueError(f"Mesh domain for {layer.key} exceeds 32 million pixels")
    out = Image.new("RGBA", (right - left, bottom - top))
    out.paste(pixels, (x0 - left, y0 - top))
    mask = np.zeros((bottom - top, right - left), dtype=bool)
    mask[y0 - top : y1 - top, x0 - left : x1 - left] = region.mask
    return out, Region(domain, mask, region.area)


def _refined_surface(
    work, layer, owner, label, geometry, allow_outside, backdrop, image_for
):
    matte = work.load(layer.edge_matte)
    if matte["kind"] != "image_split_matte" or matte["parent"] != layer.selection:
        raise ValueError(f"Refine the exact selection used by {layer.key}")
    x, y, r, b = matte["source_box"]
    rgba = image_for(layer.edge_matte).copy()
    labels = owner[y:b, x:r]
    if rgba.shape[:2] != labels.shape:
        raise ValueError("Refined matte does not match its source coordinates")
    allowed = labels == label
    if allow_outside:
        allowed |= labels == 0
    if layer.mesh is not None:
        # The existing lattice still determines the surface. A caller can reserve
        # room for exterior feathering with an explicit mesh.domain at bind time.
        left, top, right, bottom = geometry.box
        area = np.zeros(labels.shape, dtype=bool)
        x0, y0, x1, y1 = max(x, left), max(y, top), min(r, right), min(b, bottom)
        if x0 < x1 and y0 < y1:
            area[y0 - y : y1 - y, x0 - x : x1 - x] = True
        allowed &= area
    clipped = int(np.count_nonzero((rgba[..., 3] > 0) & ~allowed))
    rgba[~allowed] = 0
    visible = rgba[..., 3] > 0
    crop = Region.from_mask(visible)
    if crop is None:
        raise ValueError(f"Refined matte leaves no drawable pixels for {layer.key}")
    if backdrop is not None:
        backdrop[y:b, x:r] |= visible
    left, top, right, bottom = crop.box
    region = Region((x + left, y + top, x + right, y + bottom), crop.mask, crop.area)
    return Image.fromarray(rgba[top:bottom, left:right]), region, clipped


def build(
    work,
    base_id,
    layers,
    groups,
    duration,
    backing=None,
    *,
    shadows=None,
    seams=None,
    mattes=None,
    camera=None,
):
    recipe = plan_scene(
        dict(
            base=base_id,
            layers=layers,
            groups=groups,
            duration=duration,
            backing=backing,
            claim_order=[layer.key for layer in layers],
            shadows=shadows or [],
            seams=seams or [],
            mattes=mattes or [],
            camera=camera,
        )
    )["recipe"]
    return _assemble(work, recipe)


def _motion_provenance(record, provenance):
    if provenance is None:
        return record
    inputs = dict(record.get("inputs", {}))
    inputs["motion_reference"] = provenance["reference"]
    record["inputs"] = inputs
    record["motion_retarget"] = provenance
    return record


def _validate_projection(width, height, nodes, camera):
    focal = camera.focal if camera and camera.focal else 2 * max(width, height)
    dollies = [0, *(key.dolly for key in camera.motion)] if camera else [0]
    for dolly in dollies:
        if focal - dolly <= 1:
            raise ValueError("Camera dolly crosses the projection plane")
        for node in nodes:
            if focal - dolly - node.plane_depth <= 1:
                raise ValueError(
                    f"Node {node.key} crosses the camera projection plane"
                )


def _assemble(work, recipe, previous=None, provenance=None):
    base_id, duration, backing = (
        recipe["base"],
        float(recipe["duration"]),
        recipe["backing"],
    )
    layers = [Cutout.model_validate(layer) for layer in recipe["layers"]]
    groups = [Node.model_validate(group) for group in recipe["groups"]]
    shadows = [SoftShadow.model_validate(shadow) for shadow in recipe["shadows"]]
    seams = [Seam.model_validate(seam) for seam in recipe["seams"]]
    mattes = [LocalMatte.model_validate(matte) for matte in recipe["mattes"]]
    camera = (
        CameraTrack.model_validate(recipe["camera"])
        if recipe.get("camera") is not None
        else None
    )
    priority = recipe["claim_order"]
    base = work.load(base_id)
    base_source = base["parent"] if base["kind"] == "image_split" else base_id
    images = {}

    def image_for(source_id):
        if source_id not in images:
            images[source_id] = np.asarray(source_rgba(work, work.load(source_id)))
        return images[source_id]

    pixels = image_for(base_source)
    height, width = pixels.shape[:2]
    _validate_projection(width, height, [*groups, *layers], camera)
    specs = {layer.key: layer for layer in layers}
    ownership = {base_source: np.zeros((height, width), np.uint32)}
    masks = {}
    for i, key in enumerate(priority, 1):
        layer = specs[key]
        if layer.scene is not None:
            child = _validate_scene_dependency(work, layer.scene)
            region = Region(
                (0, 0, child["width"], child["height"]),
                np.ones((child["height"], child["width"]), dtype=bool),
                child["width"] * child["height"],
            )
            masks[key] = (layer.scene, region, None, 0)
            continue
        if layer.source is not None:
            pixels_for_layer = image_for(layer.source)
            region = Region.from_mask(pixels_for_layer[..., 3] > 0)
            masks[key] = (layer.source, region, None, 0)
            continue
        selection = work.load(layer.selection)
        source_id = _lineage(work, selection)
        image_h, image_w = image_for(source_id).shape[:2]
        left, top, right, bottom = selection["source_box"]
        local = selection_mask(work, selection)
        if not (0 <= left < right <= image_w and 0 <= top < bottom <= image_h):
            raise ValueError("Invalid cutout bounds")
        if local.shape != (bottom - top, right - left):
            raise ValueError("Cutout mask dimensions do not match its box")
        if source_id not in ownership:
            ownership[source_id] = np.zeros((image_h, image_w), np.uint32)
        owner = ownership[source_id]
        owner[top:bottom, left:right][local] = i
        masks[key] = source_id, None, (owner, i), int(local.sum())

    files = {}
    owner = ownership[base_source]
    removed_from_base = owner != 0
    catalog = []
    regions = {}
    group_by_key = {group.key: group for group in groups}
    for layer in layers:
        source_id, region, slot, requested = masks[layer.key]
        if layer.scene is not None:
            dx, dy = layer.offset
            left, top, right, bottom = region.box
            regions[layer.key] = region
            catalog.append(
                layer.model_dump(exclude={"selection", "source", "scene", "mesh"})
                | {
                    "scene_id": layer.scene,
                    "box": [left + dx, top + dy, right + dx, bottom + dy],
                    "source_box": list(region.box),
                    "source_id": layer.scene,
                    "pixels": region.area,
                    "taken_by_other_cutouts": 0,
                }
            )
            continue
        if slot is not None:
            layer_owner, label = slot
            region = Region.from_mask(layer_owner == label)
        if region is None:
            raise ValueError(
                f"Cutout {layer.key} has no pixels after ownership assignment"
            )
        file = f"cutout-{layer.key}.png"
        owned = region
        cropped, region, underlap_pixels = _with_paint(
            layer, _rgba_part(image_for(source_id), owned), owned, image_for
        )
        cropped, region = _align_skin_domain(layer, cropped, region)
        geometry = region
        if layer.edge_matte is not None:
            edited, edited_region, clipped = _refined_surface(
                work,
                layer,
                layer_owner,
                label,
                geometry,
                source_id != base_source or backing is not None,
                removed_from_base if source_id == base_source else None,
                image_for,
            )
            cropped, region, underlap_pixels = _with_paint(
                layer, edited, edited_region, image_for
            )
            if layer.mesh is not None:
                left, top, right, bottom = geometry.box
                fitted = Image.new("RGBA", (right - left, bottom - top))
                fitted.paste(cropped, (region.box[0] - left, region.box[1] - top))
                cropped, region = fitted, geometry
        regions[layer.key] = region
        files[file] = (
            apply_grade(cropped, layer.appearance.grade)
            if layer.appearance
            else cropped
        )
        dx, dy = layer.offset
        left, top, right, bottom = region.box
        catalog.append(
            layer.model_dump(exclude={"selection", "source", "scene", "mesh"})
            | {
                "file": file,
                "box": [left + dx, top + dy, right + dx, bottom + dy],
                "source_box": list(region.box),
                "source_id": source_id,
                "pixels": owned.area,
                "taken_by_other_cutouts": requested - owned.area
                if slot is not None
                else 0,
            }
        )
        if (
            layer.underlap
            or layer.paintover
            or layer.edge_matte
            or (layer.mesh and layer.mesh.domain)
        ):
            catalog[-1].update(
                owned_source_box=list(owned.box),
                underlap_pixels=underlap_pixels,
            )
        if layer.edge_matte is not None:
            catalog[-1]["edge_matte_clipped_pixels"] = clipped
        if layer.mesh is not None:
            catalog[-1]["mesh"] = _bind_mesh(
                work, layer.mesh, region, group_by_key, source_id, layer.offset
            )
    for group in groups:
        if group.mount is None:
            continue
        region = regions[group.mount.layer]
        left, top, right, bottom = region.box
        for x, y in (group.mount.point, group.mount.towards):
            if not (
                left <= x < right
                and top <= y < bottom
                and region.mask[math.floor(y) - top, math.floor(x) - left]
            ):
                raise ValueError(
                    f"Mount {group.key} requires two points on the drawable surface of {group.mount.layer}"
                )

    background = pixels.copy()
    background[removed_from_base] = 0
    files["background.png"] = Image.fromarray(background)
    if backing is not None:
        item = work.load(backing)
        fill = source_rgba(work, item)
        if fill.size != (width, height):
            raise ValueError(
                "Backing must be aligned and match the complete scene dimensions"
            )
        fill_pixels = np.array(fill)
        fill_pixels[~removed_from_base] = 0
        files["backing.png"] = Image.fromarray(fill_pixels)
    seam_specs = []
    if seams:
        seam_layers = {
            edge.layer for seam in seams for edge in (seam.a, seam.b)
        }
        drawable = {}
        for item in catalog:
            if item["key"] not in seam_layers:
                continue
            if "file" not in item:
                raise ValueError(
                    f"Seam layer {item['key']} must be a raster cutout, not a scene source"
                )
            drawable[item["key"]] = (files[item["file"]], item["source_box"])
        for seam in seams:
            ribbon, texture = build_seam(seam, drawable)
            seam_specs.append(ribbon)
            files[ribbon["file"]] = texture
    spec = dict(
        schema=1,
        width=width,
        height=height,
        duration=duration,
        background="background.png",
        backing="backing.png" if backing is not None else None,
        groups=[group.model_dump() for group in groups],
        layers=catalog,
        shadows=[shadow.model_dump() for shadow in shadows],
    )
    if seam_specs:
        spec["seams"] = seam_specs
    if mattes:
        spec["mattes"] = [matte.model_dump() for matte in mattes]
    if camera is not None:
        spec["camera"] = camera.model_dump()
    files["scene.json"] = json.dumps(spec, ensure_ascii=False, indent=2).encode()
    files["cutout-scene.mjs"] = (
        Path(__file__).with_name("cutout-scene.mjs").read_bytes()
    )
    files["cutout-timeline.mjs"] = (
        Path(__file__).with_name("cutout-timeline.mjs").read_bytes()
    )
    files["scene-evaluate.mjs"] = (
        Path(__file__).with_name("scene-evaluate.mjs").read_bytes()
    )
    if seams or any(
        layer.mesh is not None or (layer.appearance and layer.appearance.light)
        for layer in layers
    ):
        files["cutout-mesh.mjs"] = (
            Path(__file__).with_name("cutout-mesh.mjs").read_bytes()
        )
    if seams:
        files["cutout-seams.mjs"] = (
            Path(__file__).with_name("cutout-seams.mjs").read_bytes()
        )
    if shadows:
        files["cutout-shadows.mjs"] = (
            Path(__file__).with_name("cutout-shadows.mjs").read_bytes()
        )
    record = dict(
        kind="cutout_scene",
        origin="derived",
        parent=previous or base_id,
        base=base_id,
        inputs={f"source_{i}": item for i, item in enumerate(images)},
        width=width,
        height=height,
        duration=duration,
        groups=spec["groups"],
        layers=[layer.model_dump() for layer in layers],
        backing=backing,
        claim_order=priority,
        cutouts=catalog,
        shadows=spec["shadows"],
        camera=spec.get("camera"),
        **({"seams": [seam.model_dump() for seam in seams]} if seams else {}),
        **({"mattes": spec["mattes"]} if mattes else {}),
        backdrop_pixels=int(np.count_nonzero(owner == 0)),
    )
    return work.create(_motion_provenance(record, provenance), files)


def _revise_timeline(work, scene, recipe, provenance=None):
    spec = json.loads(work.path(scene["files"]["scene.json"]).read_text("utf-8"))
    timeline = {layer["key"]: layer for layer in recipe["layers"]}
    spec.update(duration=float(recipe["duration"]), groups=recipe["groups"])
    spec["layers"] = [
        layer
        | {
            "motion": timeline[layer["key"]].get("motion", []),
            "opacity": timeline[layer["key"]].get("opacity", []),
            "blend_mode": timeline[layer["key"]].get("blend_mode", "normal"),
            "plane_depth": timeline[layer["key"]].get("plane_depth", 0),
            "local_time": timeline[layer["key"]].get("local_time"),
        }
        for layer in spec["layers"]
    ]
    if recipe.get("camera") is None:
        spec.pop("camera", None)
    else:
        spec["camera"] = recipe["camera"]
    record = {key: value for key, value in scene.items() if key not in ("id", "files")}
    record.update(
        parent=scene["id"],
        groups=recipe["groups"],
        layers=recipe["layers"],
        duration=spec["duration"],
        cutouts=spec["layers"],
        camera=spec.get("camera"),
    )
    _motion_provenance(record, provenance)
    # A new, portable bundle with byte-identical rasters; no mask/mesh rebuild.
    files = {
        name: work.path(path).read_bytes()
        for name, path in scene["files"].items()
        if name != "scene.json"
    }
    for name in (
        "cutout-scene.mjs",
        "cutout-timeline.mjs",
        "scene-evaluate.mjs",
        "cutout-seams.mjs",
    ):
        if name != "cutout-seams.mjs" or name in files:
            files[name] = Path(__file__).with_name(name).read_bytes()
    files["scene.json"] = json.dumps(spec, ensure_ascii=False, indent=2).encode()
    return work.create(record, files)


def revise(work, scene, *, provenance=None, **patch):
    if scene["kind"] != "cutout_scene":
        raise ValueError("Select an existing cutout scene")
    original = {
        key: scene[key] for key in ("groups", "layers", "duration", "claim_order")
    }
    original.update(
        base=scene.get("base") or scene["split"], backing=scene.get("backing")
    )
    original.update({key: scene.get(key, []) for key in ("shadows", "seams", "mattes")})
    original["camera"] = scene.get("camera")
    result = plan_scene(original, patch)
    if result["timelineOnly"]:
        camera = (
            CameraTrack.model_validate(result["recipe"]["camera"])
            if result["recipe"].get("camera") is not None
            else None
        )
        _validate_projection(
            scene["width"],
            scene["height"],
            [
                *[
                    Node.model_validate(group)
                    for group in result["recipe"]["groups"]
                ],
                *[
                    Cutout.model_validate(layer)
                    for layer in result["recipe"]["layers"]
                ],
            ],
            camera,
        )
        return _revise_timeline(work, scene, result["recipe"], provenance)
    return _assemble(
        work, result["recipe"], previous=scene["id"], provenance=provenance
    )
