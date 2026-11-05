#!/usr/bin/env python3
"""Measured forecast benchmark on SYNTHETIC load / solar / wind series (see docs/BENCHMARKS.md). No real utility data."""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "historian"))
from hist import forecast as fc, synth

if __name__ == "__main__":
    rows = []
    for name, gen, kw in (("load", synth.load, {}), ("solar", synth.solar, {"nonneg": True}), ("wind", synth.wind, {"nonneg": True})):
        for days in (14, 60):
            for hor in (60, 360):
                cov80, cov95, skills, models = [], [], [], []
                t_run = 0.0
                for seed in range(1, 9):                    # 8 independent synthetic realisations
                    t, v = gen(days=days, step_min=5, seed=seed)
                    t0 = time.perf_counter(); r = fc.forecast(t, v, step_min=5, horizon_min=hor, **kw); t_run += time.perf_counter() - t0
                    b = r["backtest"]; cov80.append(b["coverage"]["80"]); cov95.append(b["coverage"]["95"])
                    skills.append(b["skill_vs_persistence"][r["model"]]); models.append(r["model"])
                row = dict(series=name, days=days, horizon_min=hor, runs=8, cov80_mean=round(sum(cov80) / 8, 3), cov80_min=round(min(cov80), 3),
                           cov95_mean=round(sum(cov95) / 8, 3), cov95_min=round(min(cov95), 3), skill_mean=round(sum(skills) / 8, 3), skill_min=round(min(skills), 3),
                           models={m: models.count(m) for m in set(models)}, seconds_per_forecast=round(t_run / 8, 3))
                rows.append(row); print(json.dumps(row), flush=True)
    t, v = synth.load(days=365, step_min=5)
    t0 = time.perf_counter(); fc.forecast(t, v, step_min=5, horizon_min=360); dt = time.perf_counter() - t0
    big = dict(series="load", days=365, samples=len(t), seconds_per_forecast=round(dt, 2)); rows.append(big); print(json.dumps(big))
    t, v = synth.load(days=14, step_min=5)
    t0 = time.perf_counter()
    for _ in range(100): fc.forecast(t, v, step_min=5, horizon_min=120)
    per = (time.perf_counter() - t0) / 100
    fleet = dict(note="1,000 series x 14 days, extrapolated from 100 measured runs", seconds_per_forecast=round(per, 3), fleet_1000_serial_s=round(per * 1000, 1)); rows.append(fleet); print(json.dumps(fleet))
    json.dump(rows, open(os.path.join(ROOT, "docs", "bench_forecast.json"), "w"), indent=1)
