import numpy as np

from edge_matte import refine_rgba


def test_recovers_subpixel_edge_without_carrying_the_old_background():
    fg = np.array([37, 69, 109])
    bg = np.array([237, 225, 201])
    y, x = np.indices((48, 64))
    a = np.clip(24.8 + y * 0.42 - x, 0, 1)
    rgba = np.full((48, 64, 4), 255, dtype=np.uint8)
    rgba[..., :3] = np.rint(a[..., None] * fg + (1 - a[..., None]) * bg)
    hard = a >= 0.7
    refined = refine_rgba(rgba, hard, 4)

    np.testing.assert_allclose(refined[..., 3] / 255, a, atol=0.04)
    soft = (a > 0.2) & (a < 0.8)
    assert np.max(np.abs(refined[soft, :3] - fg)) <= 2
    np.testing.assert_array_equal(refined[a == 1], rgba[a == 1])
    assert (refined[a == 0] == 0).all()


def test_keeps_unresolved_edges_and_source_transparency():
    rgba = np.full((32, 32, 4), 255, dtype=np.uint8)
    mask = np.indices((32, 32))[1] < 16
    rgba[..., :3] = 30
    rgba[mask, :3] = 35
    rgba[12:18, 13:16, 3] = 91
    expected = rgba.copy()
    expected[~mask] = 0
    np.testing.assert_array_equal(refine_rgba(rgba, mask, 4), expected)
