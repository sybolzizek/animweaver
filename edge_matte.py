"""Refine a selected visible silhouette against the source raster."""

import cv2
import numpy as np


def _distance(mask):
    return cv2.distanceTransform(
        mask.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE
    )


def _nearest(seeds, y, x):
    distances, labels = cv2.distanceTransformWithLabels(
        (~seeds).astype(np.uint8),
        cv2.DIST_L2,
        cv2.DIST_MASK_5,
        labelType=cv2.DIST_LABEL_PIXEL,
    )
    lookup = np.empty(int(labels.max()) + 1, dtype=np.intp)
    locations = np.flatnonzero(seeds)
    lookup[labels.ravel()[locations]] = locations
    return lookup[labels[y, x]], distances[y, x]


def refine_rgba(rgba, mask, band_px):
    """Estimate coverage and uncontaminated foreground only inside an uncertainty band."""
    result = rgba.copy()
    result[~mask] = 0
    if not mask.any() or mask.all():
        return result

    inside = _distance(mask)
    outside = _distance(~mask)
    opaque = rgba[..., 3] == 255
    foreground = mask & (inside > band_px) & opaque
    background = (~mask) & (outside > band_px) & opaque
    if not foreground.any() or not background.any():
        return result

    y, x = np.nonzero(((inside + outside) <= band_px) & opaque)
    fg_idx, fg_dist = _nearest(foreground, y, x)
    bg_idx, bg_dist = _nearest(background, y, x)
    nearby = (fg_dist <= 2 * band_px + 1) & (bg_dist <= 2 * band_px + 1)
    y, x = y[nearby], x[nearby]
    flat = rgba.reshape(-1, 4)
    fg = flat[fg_idx[nearby], :3].astype(np.float32)
    bg = flat[bg_idx[nearby], :3].astype(np.float32)
    color = rgba[y, x, :3].astype(np.float32)

    # Solve C = a F + (1-a) B in local source RGB. If the colors are close
    # or the pixel does not fit that mixture, preserve the hard assignment.
    axis = fg - bg
    contrast2 = np.einsum("ij,ij->i", axis, axis)
    a = np.clip(
        np.einsum("ij,ij->i", color - bg, axis) / np.maximum(contrast2, 1), 0, 1
    )
    residual = color - (bg + a[:, None] * axis)
    error = np.linalg.norm(residual, axis=1)
    # A small tolerance allows source compression and shading. Do not feather
    # low-contrast edges simply because the silhouette is uncertain.
    accepted = (contrast2 >= 24**2) & (
        error <= np.minimum(18, 5 + 0.10 * np.sqrt(contrast2))
    )
    y, x, a = y[accepted], x[accepted], a[accepted]
    a[a < 0.035] = 0
    a[a > 0.965] = 1
    # Discard the background contribution, retaining a bounded residual rather
    # than amplifying small color errors at nearly transparent pixels.
    recovered = fg[accepted] + residual[accepted] / np.maximum(a[:, None], 0.35)
    recovered[a == 1] = color[accepted][a == 1]
    recovered[a == 0] = 0
    result[y, x, :3] = np.rint(np.clip(recovered, 0, 255)).astype(np.uint8)
    result[y, x, 3] = np.rint(a * 255).astype(np.uint8)
    # A similar-looking background pixel can fit the color line even when it
    # is not adjacent to the cutout. Prevent a low-opacity halo from spreading
    # across the whole search band around a solid object.
    weak = (a > 0) & (a < 0.5) & (outside[y, x] > 0)
    if weak.any():
        support = result[..., 3] >= 128
        if support.any():
            isolated = weak & (_distance(~support)[y, x] > 1.5)
            result[y[isolated], x[isolated]] = 0
    return result
