"""Recursive ownership of visible source pixels; independent of animation hierarchy."""

import hashlib
from typing import Annotated

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from image_split import (
    Region,
    _rgba_part,
    _anchor,
    overlay,
    selection_mask,
    source_rgba,
)


class ChildCut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
    selection: str | None = None
    polygon: Annotated[list[tuple[int, int]], Field(min_length=3)] | None = None

    @model_validator(mode="after")
    def one_basis(self):
        if (self.selection is None) == (self.polygon is None):
            raise ValueError("Supply exactly one hard selection or polygon per child")
        return self


def _selection(work, item, split):
    if item["kind"] != "image_split_selection" or item["parent"] != split["id"]:
        raise ValueError("Use a hard selection from this tree's original split")
    left, top, right, bottom = item["source_box"]
    if not (
        0 <= left < right <= split["width"] and 0 <= top < bottom <= split["height"]
    ):
        raise ValueError("Cutout bounds do not match the source")
    mask = selection_mask(work, item)
    if mask.shape != (bottom - top, right - left):
        raise ValueError("Cutout mask dimensions do not match its box")
    return Region((left, top, right, bottom), mask, int(mask.sum()))


def _persist(work, pixels, region, split_id, path, basis, request=None):
    left, top, right, bottom = region.box
    fields = dict(
        kind="image_split_selection",
        origin="derived",
        parent=split_id,
        width=right - left,
        height=bottom - top,
        source_box=list(region.box),
        pixels=region.area,
        method="tree_partition",
        node_path=path,
        mask_sha256=hashlib.sha256(region.mask.tobytes()).hexdigest(),
    )
    references = {}
    if basis is not None:
        references["partition_basis"] = basis
    if request is not None and request.selection is not None:
        references["candidate"] = request.selection
    if references:
        fields["inputs"] = references
    if request is not None and request.polygon is not None:
        fields["polygon"] = request.polygon
    row = work.create(
        fields,
        {
            "image.png": _rgba_part(pixels, region),
            "mask.png": Image.fromarray(region.mask.astype(np.uint8) * 255),
        },
    )
    return {
        "selection": row["id"],
        "box": list(region.box),
        "pixels": region.area,
        "children": [],
    }


def _tree_node(work, tree, branch):
    if tree["kind"] != "image_cutout_tree" or branch not in tree["nodes"]:
        raise ValueError("Select an existing cutout tree and branch path")
    split = work.load(tree["split"])
    return split, tree["nodes"][branch]


def _node_mask(work, node, split):
    full = np.zeros((split["height"], split["width"]), dtype=bool)
    if node is not None:
        if node["selection"] is None:  # Full-image root.
            full[:] = True
        else:
            part = _selection(work, work.load(node["selection"]), split)
            part.within(full)[:] = part.mask
    return full


def export_region(work, tree, branch, padding, box):
    split, node = _tree_node(work, tree, branch)
    source = source_rgba(work, work.load(split["parent"]))
    left, top, right, bottom = node["box"]
    box = box or (
        max(0, left - padding),
        max(0, top - padding),
        min(source.width, right + padding),
        min(source.height, bottom + padding),
    )
    left, top, right, bottom = work.box(box, *source.size)
    mask = _node_mask(work, node, split)[top:bottom, left:right]
    return work.create(
        dict(
            kind="cutout_region_export",
            origin="derived",
            parent=tree["id"],
            branch=branch,
            split=split["id"],
            source_box=[left, top, right, bottom],
            selection=node["selection"],
        ),
        {
            "image.png": source.crop((left, top, right, bottom)),
            "mask.png": Image.fromarray(mask.astype(np.uint8) * 255),
        },
    )


def _update_region(work, pixels, split, nodes, path, mask, parent_basis, request=None):
    old = nodes.get(path)
    old_mask = _node_mask(work, old, split)
    if np.array_equal(old_mask, mask):
        return
    region = Region.from_mask(mask)
    if region is None:
        for key in [key for key in nodes if key == path or key.startswith(path + "/")]:
            nodes.pop(key)
        return
    new = _persist(work, pixels, region, split["id"], path, parent_basis, request)
    nodes[path] = new
    if not old or not old["children"]:
        return
    rest_path = path + "/@rest"
    rest = mask.copy()
    for child in old["children"]:
        if child == rest_path:
            continue
        keep = _node_mask(work, nodes[child], split) & mask
        _update_region(work, pixels, split, nodes, child, keep, new["selection"])
        rest &= ~keep
    _update_region(work, pixels, split, nodes, rest_path, rest, new["selection"])
    new["children"] = [
        child for child in old["children"] if child in nodes and child != rest_path
    ]
    if rest_path in nodes:
        new["children"].append(rest_path)


def replace_region(work, tree, branch, selection, box, preview_only):
    split, old = _tree_node(work, tree, branch)
    # The root is the scope, and @rest is derived from the explicit sibling masks.
    if branch == "root" or branch.endswith("/@rest"):
        raise ValueError(
            "Replace an explicit child; root and @rest can be split instead"
        )
    parent_path, name = branch.rsplit("/", 1)
    parent = tree["nodes"][parent_path]
    rest_path = parent_path + "/@rest"
    width, height = split["width"], split["height"]
    if selection is None and box is None:
        raise ValueError("Specify the source box when clearing part of a branch")
    item = work.load(selection) if selection is not None else {}
    edit_box = box or item.get("inputs", {}).get("input_box") or (0, 0, width, height)
    left, top, right, bottom = work.box(edit_box, width, height)
    window = np.s_[top:bottom, left:right]
    original = _node_mask(work, old, split)
    rest = _node_mask(work, tree["nodes"].get(rest_path), split)
    candidate = np.zeros_like(original)
    if item:
        region = _selection(work, item, split)
        region.within(candidate)[:] = region.mask
    permitted = original | rest
    parent_mask = _node_mask(work, parent, split)
    edited = original.copy()
    edited[window] = candidate[window] & permitted[window]
    if not edited.any():
        raise ValueError("Replacement would remove every pixel of this branch")
    added, removed = edited & ~original, original & ~edited
    pixels = np.asarray(source_rgba(work, work.load(split["parent"])))
    area = Region.from_mask(original | edited).box
    x, y, u, v = area
    colors = pixels[y:v, x:u].copy()
    for mask, color in ((added, (42, 215, 150)), (removed, (251, 81, 93))):
        on = mask[y:v, x:u]
        colors[on, :3] = colors[on, :3] // 3 + np.array(color, dtype=np.uint8) // 3 * 2
        colors[on, 3] = 255
    diff = work.create(
        dict(
            kind="cutout_region_diff",
            origin="derived",
            parent=tree["id"],
            branch=branch,
            selection=selection,
            source_box=list(area),
            edit_box=[left, top, right, bottom],
            added=int(added.sum()),
            removed=int(removed.sum()),
            ignored_outside_parent=int(
                (candidate[window] & ~parent_mask[window]).sum()
            ),
            blocked_by_siblings=int(
                (candidate[window] & parent_mask[window] & ~permitted[window]).sum()
            ),
        ),
        {"preview.png": Image.fromarray(colors)},
    )
    if preview_only:
        return diff
    nodes = dict(tree["nodes"])
    if diff["added"] or diff["removed"]:
        _update_region(
            work,
            pixels,
            split,
            nodes,
            branch,
            edited,
            parent["selection"],
            ChildCut(key=name, selection=selection) if selection is not None else None,
        )
        _update_region(
            work,
            pixels,
            split,
            nodes,
            rest_path,
            (rest | removed) & ~added,
            parent["selection"],
        )
        nodes[parent_path] = parent | {
            "children": [child for child in parent["children"] if child != rest_path]
            + ([rest_path] if rest_path in nodes else [])
        }
    updated = (
        work.create(
            dict(
                kind="image_cutout_tree",
                origin="derived",
                parent=tree["id"],
                split=split["id"],
                width=width,
                height=height,
                changed_branch=branch,
                nodes=nodes,
                diff=diff["id"],
            ),
            {"preview.png": work.path(diff["files"]["preview.png"]).read_bytes()},
        )
        if diff["added"] or diff["removed"]
        else tree
    )
    return {
        **diff,
        "tree": updated["id"],
        "node": nodes[branch],
        "dropped_branches": sorted(tree["nodes"].keys() - nodes.keys()),
    }


def split_branch(work, start, parent, cuts, replace):
    if not cuts or len({cut.key for cut in cuts}) != len(cuts):
        raise ValueError("Provide cuts with distinct child keys")
    previous = start if start["kind"] == "image_cutout_tree" else None
    if previous:
        split = work.load(previous["split"])
        nodes = {key: dict(row) for key, row in previous["nodes"].items()}
    else:
        if start["kind"] not in {"image_split", "image_split_selection"}:
            raise ValueError(
                "Start from a split, a hard selection, or an existing cutout tree"
            )
        split = start if start["kind"] == "image_split" else work.load(start["parent"])
        nodes = {
            "root": {
                "box": start.get("source_box", [0, 0, split["width"], split["height"]]),
                "pixels": start.get("pixels", split["width"] * split["height"]),
                "selection": start["id"]
                if start["kind"] == "image_split_selection"
                else None,
                "children": [],
            }
        }
    if parent not in nodes:
        raise ValueError("Unknown parent path")
    anchor = nodes[parent]
    if anchor["children"] and not replace:
        raise ValueError(
            "This node is already partitioned; split a child or request branch replacement"
        )
    w, h = split["width"], split["height"]
    basis = anchor["selection"]
    parent_region = (
        _selection(work, work.load(basis), split)
        if basis
        else Region((0, 0, w, h), np.ones((h, w), dtype=bool), w * h)
    )
    owners = np.zeros((h, w), np.uint16)
    owners_view = parent_region.within(owners)
    requests = []
    for label, cut in enumerate(cuts, 1):
        if cut.selection is not None:
            candidate = _selection(work, work.load(cut.selection), split)
        else:
            if not all(0 <= x < w and 0 <= y < h for x, y in cut.polygon):
                raise ValueError(f"Polygon for {cut.key} must fit the original image")
            polygon = np.zeros((h, w), np.uint8)
            cv2.fillPoly(polygon, [np.asarray(cut.polygon, dtype=np.int32)], 1)
            candidate = Region.from_mask(polygon > 0)
            if candidate is None:
                raise ValueError(f"Empty polygon for {cut.key}")
        candidate_full = np.zeros((h, w), dtype=bool)
        candidate.within(candidate_full)[:] = candidate.mask
        inside = parent_region.within(candidate_full) & parent_region.mask
        requests.append((cut, candidate.area, int(inside.sum())))
        owners_view[inside] = label

    planned = []
    for i, (cut, requested, inside) in enumerate(requests, 1):
        region = Region.from_mask(owners == i)
        if region is None:
            raise ValueError(f"Cut {cut.key} owns no pixels inside the parent")
        planned.append(
            (
                cut.key,
                region,
                cut,
                {
                    "discarded_outside_parent": requested - inside,
                    "lost_to_siblings": inside - region.area,
                },
            )
        )
    local_rest = parent_region.mask & (owners_view == 0)
    if local_rest.any():
        label = len(planned) + 1
        owners_view[local_rest] = label
        rest = Region.from_mask(owners == label)
        planned.append(("@rest", rest, None, {}))
    if sum(item[1].area for item in planned) != parent_region.area:
        raise ValueError("The branch does not cover its parent")

    rgba = np.asarray(source_rgba(work, work.load(split["parent"])))
    for old in [path for path in nodes if path.startswith(parent + "/")]:
        nodes.pop(old)
    children, annotations = [], []
    ox, oy, _, _ = parent_region.box
    for name, region, request, counts in planned:
        path = f"{parent}/{name}"
        node = _persist(work, rgba, region, split["id"], path, basis, request)
        nodes[path] = node | counts
        children.append(path)
        x, y = _anchor(region)
        annotations.append(
            {"id": name, "pixels": region.area, "point": [x - ox, y - oy]}
        )
    nodes[parent] = anchor | {"children": children}
    record = work.create(
        dict(
            kind="image_cutout_tree",
            origin="derived",
            parent=start["id"],
            split=split["id"],
            width=w,
            height=h,
            changed_branch=parent,
            nodes=nodes,
        ),
        {"preview.png": overlay(parent_region.within(rgba), owners_view, annotations)},
    )
    return {
        "id": record["id"],
        "split": split["id"],
        "branch": parent,
        "parent_box": anchor["box"],
        "children": [{"path": key, **nodes[key]} for key in children],
        "overview": record["files"]["preview.png"],
    }
