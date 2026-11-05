#!/usr/bin/env python3
"""Seed the historian with 3 days of SYNTHETIC demo series (SYN.LOAD.P, SYN.SOLAR.P, SYN.WIND.P) so the forecast screen has
daily-seasonal history to learn from on a freshly started demo stack. Clearly named; never mixed into real telemetry tags."""
import os, sys, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "services", "historian"))
from hist.store import Store
from hist import synth

if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gs/historian.db"
    if db.startswith("postgresql://"):
        from hist.pgstore import PgStore
        s = PgStore(db)
    else:
        s = Store(db)
    end = int(time.time() * 1000) - 30 * 60_000
    n = 0
    for tag, gen in (("SYN.LOAD.P", synth.load), ("SYN.SOLAR.P", synth.solar), ("SYN.WIND.P", synth.wind)):
        t, v = gen(days=3, step_min=1)
        t = t - t[-1] + end                                  # shift so the series ends 30 minutes ago
        # the hour of day must stay consistent: realign by whole days
        pts = [{"t": tag, "m": int(a), "v": float(b)} for a, b in zip(t, v)]
        n += s.write(pts)
    print(f"seeded {n} synthetic rows")
