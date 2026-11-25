"""Image-space registration and evidence displays, independent of any renderer."""

from typing import Literal

import cv2
import numpy as np
from PIL import Image, ImageDraw
from pydantic import BaseModel


class PointPair(BaseModel):
    name: str
    reference: tuple[float, float]
    candidate: tuple[float, float]


Alignment = Literal["identity", "translation", "similarity"]


def point(value, size):
    x, y = value
    if not (0 <= x < size[0] and 0 <= y < size[1]):
        raise ValueError(f"Point {value} does not fit the image of size {size}")
    return x, y


def transform(anchors, alignment):
    if alignment == "identity":
        if anchors:
            raise ValueError("Identity takes no anchors")
        return np.eye(2, 3)
    if len(anchors) < (1 if alignment == "translation" else 2):
        raise ValueError(f"Not enough anchors for {alignment} alignment")
    candidate = np.asarray([a.candidate for a in anchors], dtype=float)
    reference = np.asarray([a.reference for a in anchors], dtype=float)
    center, target = candidate.mean(axis=0), reference.mean(axis=0)
    linear = np.eye(2)
    if alignment == "similarity":
        # Least-squares uniform scale and rotation. No reflection, shear or shape warp.
        a, b = candidate - center, reference - target
        denom = np.sum(a * a)
        if denom < 1.0:
            raise ValueError("Candidate anchors are coincident or too close")
        c = float(np.sum(a * b) / denom)
        s = float(np.sum(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) / denom)
        if np.hypot(c, s) < 1e-8:
            raise ValueError("Reference anchors do not define a usable scale and rotation")
        linear = np.array([[c, -s], [s, c]])
    return np.column_stack((linear, target - linear @ center))


def residual(pair, matrix):
    p = matrix[:, :2] @ pair.candidate + matrix[:, 2]
    d = p - pair.reference
    return {**pair.model_dump(mode="json"), "candidate_aligned": p.tolist(),
            "offset_px": d.tolist(), "distance_px": float(np.linalg.norm(d))}


def mask_for(rgba, polygon):
    alpha = rgba.getchannel("A")
    if polygon is None:
        return np.asarray(alpha) >= 128, alpha.getextrema()[0] < 128
    if len(polygon) < 3:
        raise ValueError("A mask polygon requires at least three points")
    p = np.asarray([point(v, rgba.size) for v in polygon], dtype=float)
    if abs(cv2.contourArea(p.astype(np.float32))) < 0.5:
        raise ValueError("Mask polygon has zero area")
    selection = Image.new("L", rgba.size)
    ImageDraw.Draw(selection).polygon([tuple(v) for v in p], fill=255)
    return (np.asarray(alpha) >= 128) & (np.asarray(selection) > 0), True


def rgba_with_mask(rgb, visible):
    return Image.fromarray(np.dstack((rgb.astype(np.uint8), visible.astype(np.uint8)*255)))


def warp(rgba, mask, matrix, size):
    rgb = np.asarray(rgba)[..., :3].astype(np.float32)
    # Interpolate premultiplied pixels to avoid leaking the background across mask edges.
    alpha = mask.astype(np.float32)
    color = cv2.warpAffine(rgb * alpha[..., None], matrix, size)
    weight = cv2.warpAffine(alpha, matrix, size)
    return np.clip(np.rint(color / np.maximum(weight[..., None], 1e-6)), 0, 255).astype(np.uint8), weight >= .5


def compare(reference, candidate, anchors, checks, alignment, focus, ref_polygon=None, cand_polygon=None):
    for group in (anchors, checks):
        if len({p.name for p in group}) != len(group):
            raise ValueError("Point names must be unique within each group")
        for pair in group:
            point(pair.reference, reference.size)
            point(pair.candidate, candidate.size)
    if set(p.name for p in anchors) & set(p.name for p in checks):
        raise ValueError("Fit anchors and inspection points must be separate")
    if any(a.reference == b.reference or a.candidate == b.candidate for a in anchors for b in checks):
        raise ValueError("An inspection point reuses a fit anchor")
    matrix = transform(anchors, alignment)
    left, top, right, bottom = focus
    size = (right-left, bottom-top)
    fit_rows = [residual(pair, matrix) for pair in anchors]
    check_rows = [residual(pair, matrix) for pair in checks]
    focus_transform = matrix.copy()
    focus_transform[:, 2] -= [left, top]

    a_mask, ref_object = mask_for(reference, ref_polygon)
    b_mask, cand_object = mask_for(candidate, cand_polygon)
    a = np.asarray(reference)[top:bottom, left:right, :3]
    am = a_mask[top:bottom, left:right]
    b, bm = warp(candidate, b_mask, focus_transform, size)
    both, either = am & bm, am | bm
    if not both.any():
        raise ValueError("Images have no common valid pixels in the focus window")
    overlay = np.where(am[...,None], a, b).copy()
    overlay[both] = np.rint((a[both].astype(float)+b[both])/2).astype(np.uint8)
    difference = cv2.absdiff(a, b)
    files = {"reference.png": rgba_with_mask(a, am), "registered.png": rgba_with_mask(b, bm),
             "overlay.png": rgba_with_mask(overlay, either), "difference.png": rgba_with_mask(difference, both)}
    metrics = {"overlap_px": int(both.sum()), "rgb_abs_mean_8bit": float(difference[both].mean()),
               "fit_rms_px": float(np.sqrt(np.mean([p["distance_px"]**2 for p in fit_rows]))) if fit_rows else None}
    if ref_object and cand_object:
        # Compute boundaries in the complete reference frame, then sample inside focus.
        # This avoids introducing false shape edges at the focus crop boundary.
        b_full = cv2.warpAffine(b_mask.astype(np.uint8), matrix, reference.size, flags=cv2.INTER_NEAREST).astype(bool)
        edges = [m & ~cv2.erode(m.astype(np.uint8), np.ones((3,3), np.uint8)).astype(bool) for m in (a_mask, b_full)]
        da, db = [cv2.distanceTransform((~e).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
                  if e.any() else None for e in edges]
        ea, eb = [e[top:bottom, left:right] for e in edges]
        metrics["mask_iou"] = int(both.sum()) / int(either.sum())
        if db is not None and ea.any():
            metrics["reference_to_candidate_boundary_px"] = float(db[top:bottom,left:right][ea].mean())
        if da is not None and eb.any():
            metrics["candidate_to_reference_boundary_px"] = float(da[top:bottom,left:right][eb].mean())
        ink = (overlay.astype(float)*.42).astype(np.uint8)
        ink[ea] = [255, 106, 126]
        ink[eb] = [80, 229, 240]
        ink[ea & eb] = [255, 255, 255]
        files["contours.png"] = rgba_with_mask(ink, either | ea | eb)
    if check_rows:
        draw_on = files["overlay.png"].copy()
        draw = ImageDraw.Draw(draw_on)
        for index, detail in enumerate(check_rows, 1):
            p = [detail["reference"][0]-left, detail["reference"][1]-top]
            q = [detail["candidate_aligned"][0]-left, detail["candidate_aligned"][1]-top]
            draw.line([tuple(p), tuple(q)], fill="#ffffff", width=1)
            for (x,y), ink in [(p,"#ff6a7e"),(q,"#50e5f0")]:
                draw.ellipse([x-2,y-2,x+2,y+2], outline=ink, width=2)
            draw.text((p[0]+5,p[1]+4), str(index), fill="white", stroke_width=1, stroke_fill="black")
        files["landmarks.png"] = draw_on
    return {"candidate_to_reference": matrix.tolist(), "fit": fit_rows, "checks": check_rows,
            "metrics": metrics}, files
