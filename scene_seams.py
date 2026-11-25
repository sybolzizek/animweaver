"""Bind a small textured bridge to two independently deformed cutout edges."""

from typing import Annotated, Literal

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


Name = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Point = tuple[FiniteFloat, FiniteFloat]
Time = Annotated[FiniteFloat, Field(ge=0)]


class SeamEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Name
    rail: Annotated[list[Point], Field(min_length=2, max_length=16)]


class Seam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Name
    a: SeamEdge
    b: SeamEdge
    texture_from: Literal["blend", "a", "b"] = "blend"
    max_span: Annotated[FiniteFloat, Field(gt=0)]
    windows: list[tuple[Time, Time]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check(self):
        if self.a.layer == self.b.layer or len(self.a.rail) != len(self.b.rail):
            raise ValueError(
                "A seam requires two different layers and corresponding rail points"
            )
        last = 0
        for start, end in self.windows:
            if start < last or end <= start:
                raise ValueError(
                    "Seam windows must be ordered, nonoverlapping and have duration"
                )
            last = end
        return self


def _sample(image, box, points, key):
    # Source-image coordinates describe pixel cells; OpenCV locates their centers at integers.
    pixels = np.asarray(image)
    rel = np.asarray(points, np.float32) - (box[0] + 0.5, box[1] + 0.5)
    if (rel < -0.5).any() or (
        rel >= [pixels.shape[1] - 0.5, pixels.shape[0] - 0.5]
    ).any():
        raise ValueError(f"Seam {key} rail leaves its source cutout")
    result = cv2.remap(
        pixels,
        rel[:, 0].astype("float32").reshape(1, -1),
        rel[:, 1].astype("float32").reshape(1, -1),
        cv2.INTER_LINEAR,
    )[0]
    if (result[:, 3] < 220).any():
        raise ValueError(
            f"Seam {key} rail crosses transparent pixels; move it inside the material"
        )
    return result[:, :3].astype(float)


def build(seam, sources):
    rails = [np.asarray(edge.rail, dtype=float) for edge in (seam.a, seam.b)]
    distances = np.stack(
        [np.linalg.norm(np.diff(path, axis=0), axis=1) for path in rails]
    )
    if (distances < 1).any():
        raise ValueError(f"Seam {seam.key} has coincident rail points")
    steps = np.clip(np.ceil(distances.max(axis=0) / 3), 1, 64).astype(int)
    # Use identical parameters on both rails to preserve the point correspondence.
    coords = [
        np.concatenate(
            [
                np.linspace(a, b, int(n), endpoint=False)
                for a, b, n in zip(path[:-1], path[1:], steps)
            ]
            + [path[-1:]]
        )
        for path in rails
    ]
    colors = [
        _sample(*sources[edge.layer], xy, seam.key)
        for edge, xy in zip((seam.a, seam.b), coords)
    ]
    if seam.texture_from == "a":
        colors[1] = colors[0]
    elif seam.texture_from == "b":
        colors[0] = colors[1]
    # Put sampled colors at the centers of their texture rows. The RGB is opaque;
    # only the authored rails set the strip bounds so backdrop pixels cannot bleed in.
    height = len(coords[0]) - 1
    bands = [(c[:-1] + c[1:]) / 2 for c in colors]
    u = (np.arange(16) + 0.5) / 16
    texture = np.uint8(
        np.rint(
            bands[0][:, None, :] * (1 - u[None, :, None])
            + bands[1][:, None, :] * u[None, :, None]
        )
    )
    spec = seam.model_dump() | {
        "file": f"seam-{seam.key}.png",
        "rails": [path.tolist() for path in coords],
        "rows": height,
    }
    return spec, Image.fromarray(texture)
