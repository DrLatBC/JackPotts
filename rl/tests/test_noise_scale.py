"""The noise-scale arithmetic on gradients with a known true direction and noise."""

from __future__ import annotations

import numpy as np
import pytest

from rl.noise_scale import noise_scale, report


def chunks(true_sq: float, noise: float, decisions: int, k: int, dim: int = 4000, seed: int = 0):
    """``k`` chunk gradient sums: true gradient of squared length ``true_sq`` plus noise whose
    total variance is ``noise`` per decision, over ``decisions`` decisions (two examples each)."""
    rng = np.random.default_rng(seed)
    g = rng.normal(size=dim)
    g *= np.sqrt(true_sq) / np.linalg.norm(g)
    per = rng.normal(size=(k, dim)) * np.sqrt(noise / decisions / dim)
    examples = np.full(k, 2.0 * decisions)
    return (g + per) * examples[:, None], examples, np.full(k, float(decisions))


def test_the_noise_scale_comes_back_from_known_gradients():
    sums, ex, dec = chunks(true_sq=1.0, noise=24000.0, decisions=3000, k=32)
    got = noise_scale(sums, ex, dec)
    assert got["noise scale"] == pytest.approx(24000, rel=0.15)
    assert got["true sq"] == pytest.approx(1.0, rel=0.15)


def test_split_halves_agree_as_the_noise_scale_predicts():
    sums, ex, dec = chunks(true_sq=1.0, noise=6000.0, decisions=3000, k=16, seed=1)
    rep = report(sums, ex, dec, boot=50)
    for row in rep["split halves"]:
        n = row["decisions per side"]
        assert row["cosine"] == pytest.approx(n / (n + 6000), abs=0.06)
    assert rep["noise scale low"] < 6000 < rep["noise scale high"]
