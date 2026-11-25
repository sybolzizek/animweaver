"""Region-based color correction and 2D scene lighting parameters."""

from typing import Annotated

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from image_split import source_rgba


class ColorGrade(BaseModel):
    """RGB gains followed by a contrast adjustment in linear light."""

    model_config = ConfigDict(extra="forbid")
    gain: tuple[Annotated[FiniteFloat, Field(gt=0, le=8)], ...] = Field(
        default=(1, 1, 1), min_length=3, max_length=3
    )
    contrast: Annotated[FiniteFloat, Field(gt=0, le=4)] = 1


class LightKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: Annotated[FiniteFloat, Field(ge=0)]
    exposure: Annotated[FiniteFloat, Field(ge=-4, le=4)] = 0
    gradient: Annotated[FiniteFloat, Field(ge=-4, le=4)] = 0


class Appearance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grade: ColorGrade | None = None
    light_axis: tuple[FiniteFloat, FiniteFloat] = (0, -1)
    light: list[LightKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_axis(self):
        if self.light and not any(self.light_axis):
            raise ValueError("A lighting axis must have a direction")
        return self


def _decode(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _encode(x):
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)


def apply_grade(image, grade):
    if grade is None or (tuple(grade.gain) == (1, 1, 1) and grade.contrast == 1):
        return image
    pixels = np.array(image)
    linear = _decode(pixels[..., :3] / 255) * np.array(grade.gain)
    linear = np.clip(0.18 + grade.contrast * (linear - 0.18), 0, 1)
    pixels[..., :3] = np.uint8(np.rint(255 * _encode(linear)))
    return Image.fromarray(pixels)


def match(work, source, source_box, reference, reference_box, strength):
    """Fit a conservative grade from two explicit samples of comparable surfaces."""

    def sample(id, box):
        image = source_rgba(work, work.load(id))
        crop = np.asarray(image.crop(work.box(box, *image.size)))
        m = crop[..., 3] >= 240
        if np.count_nonzero(m) < 16:
            raise ValueError("Choose a region with at least 16 opaque sample pixels")
        return _decode(crop[..., :3][m] / 255)

    src, ref = sample(source, source_box), sample(reference, reference_box)
    med_src, med_ref = np.median(src, axis=0), np.median(ref, axis=0)
    gain = np.clip((med_ref + 0.008) / (med_src + 0.008), 0.5, 2.0)
    weights = np.array([0.2126, 0.7152, 0.0722])
    lo_hi = np.quantile((src * gain) @ weights, [0.18, 0.82])
    ref_lo_hi = np.quantile(ref @ weights, [0.18, 0.82])
    spread = float(lo_hi[1] - lo_hi[0])
    target_spread = float(ref_lo_hi[1] - ref_lo_hi[0])
    contrast = (
        1.0
        if min(spread, target_spread) < 0.02
        else float(np.clip(target_spread / spread, 0.75, 1.30))
    )
    # Contrast pivots around 0.18 in the renderer. Recenter the gain so this
    # operation doesn't undo the median color match.
    gain *= np.clip(
        (0.18 + (med_ref - 0.18) / contrast + 0.008) / (med_ref + 0.008), 0.5, 2.0
    )
    grade = ColorGrade(
        gain=tuple(float(v) for v in np.clip(gain, 0.5, 2.0) ** strength),
        contrast=contrast**strength,
    )
    return {
        "grade": grade.model_dump(),
        "source_pixels": len(src),
        "reference_pixels": len(ref),
        "source_median_linear_rgb": med_src.round(5).tolist(),
        "reference_median_linear_rgb": med_ref.round(5).tolist(),
    }
