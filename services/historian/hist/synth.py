"""SYNTHETIC series generators for tests and benchmarks (no real utility data is used or implied)."""
from __future__ import annotations
import numpy as np


def _ar1(n, phi, sigma, rng):
    e = rng.normal(0, sigma, n)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def load(days=30, step_min=5, seed=1, base=100.0, noise=0.02):
    """Daily double-peak shape x weekly factor x AR(1) noise. Returns (t_ms, MW)."""
    rng = np.random.default_rng(seed)
    n = int(days * 1440 / step_min)
    t = 1_700_000_000_000 // 86_400_000 * 86_400_000 + np.arange(n) * step_min * 60_000
    h = (t / 3_600_000.0) % 24
    shape = 0.75 + 0.15 * np.exp(-((h - 10) / 2.5) ** 2) + 0.25 * np.exp(-((h - 20) / 2.5) ** 2) - 0.1 * np.exp(-((h - 4) / 3) ** 2)
    dow = ((t // 86_400_000) + 4) % 7
    wk = np.where(dow >= 5, 0.92, 1.0)
    return t.astype(np.int64), base * shape * wk * (1 + _ar1(n, 0.97, noise / 4, rng))


def solar(days=30, step_min=5, seed=2, cap=50.0):
    """Clear-sky half-sine between 06:00 and 18:00 local x slowly varying cloud factor."""
    rng = np.random.default_rng(seed)
    n = int(days * 1440 / step_min)
    t = 1_700_000_000_000 // 86_400_000 * 86_400_000 + np.arange(n) * step_min * 60_000
    h = (t / 3_600_000.0) % 24
    clear = np.where((h > 6) & (h < 18), np.clip(np.sin(np.pi * (h - 6) / 12), 0, None) ** 1.2, 0.0)
    day = (t // 86_400_000)
    dayfac = np.clip(0.75 + 0.2 * np.sin(day * 1.7) + rng.normal(0, 0.1, len(np.unique(day)))[np.searchsorted(np.unique(day), day)], 0.2, 1.0)
    cloud = np.clip(1.0 + _ar1(n, 0.95, 0.06, rng), 0.1, 1.1)
    return t.astype(np.int64), np.clip(cap * clear * dayfac * cloud, 0, cap)


def wind(days=30, step_min=5, seed=3, cap=80.0):
    """Cubic power curve of an AR(1) wind speed with a mild diurnal cycle."""
    rng = np.random.default_rng(seed)
    n = int(days * 1440 / step_min)
    t = 1_700_000_000_000 // 86_400_000 * 86_400_000 + np.arange(n) * step_min * 60_000
    h = (t / 3_600_000.0) % 24
    ws = np.clip(7.5 + 1.2 * np.cos(2 * np.pi * (h - 15) / 24) + _ar1(n, 0.995, 0.35, rng) * 3, 0, 30)
    p = np.where(ws < 3, 0, np.where(ws > 12, 1.0, ((ws - 3) / 9) ** 3))
    return t.astype(np.int64), np.where(ws > 25, 0, cap * p)
