#!/usr/bin/env python3
"""Soak harness: drives a steady operator/analysis workload against the running stack, samples every process and service at a
fixed interval, writes a CSV, and judges drift (memory, file descriptors, threads, queue depth, latency, error counts, disk).

  tools/soak.py --hours 168                    the 7-day run (must be started by the operator; nothing here runs for 7 days)
  tools/soak.py --minutes 6 --interval 5       a smoke run, proves the harness, proves nothing about 7 days
  tools/soak.py --analyze DIR                  re-judge an existing run
  tools/soak.py --selftest                     the drift detector against synthetic series (with and without an injected leak)

Needs a stack from tools/stack.sh (any mode); reads /proc, so it must run on the same host as the services."""
import argparse, csv, json, math, os, random, statistics, subprocess, sys, time, threading
import httpx
sys.path.insert(0, os.path.dirname(__file__))

PROCS = {  # name -> pgrep -f pattern (bracket trick keeps the pattern from matching itself)
    "coreA": r"[g]s\.core\.Main.*-id A", "coreB": r"[g]s\.core\.Main.*-id B", "coreC": r"[g]s\.core\.Main.*-id C",
    "fep": r"(^|/)bin/fep ", "cimpds": r"[c]im-pds$", "ems": r"[e]ms\.api:app", "hist": r"[h]ist\.app:app", "outstation": r"[o]utstation-sim", "field": r"[f]ield_sim\.py"}

def pid_of(pat):
    try: out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split()
    except Exception: return None
    return int(out[0]) if out else None

def proc_stats(pid):
    try:
        rss = threads = 0
        for l in open(f"/proc/{pid}/status"):
            if l.startswith("VmRSS:"): rss = int(l.split()[1]) / 1024
            elif l.startswith("Threads:"): threads = int(l.split()[1])
        return rss, len(os.listdir(f"/proc/{pid}/fd")), threads
    except Exception: return None

# ---------------------------------------------------------------- drift analysis
def slope_per_hour(ts, ys):
    """Least-squares slope (units per hour), R^2, and total relative change fitted over the series."""
    n = len(ts)
    if n < 8: return 0.0, 0.0, 0.0
    mt, my = statistics.fmean(ts), statistics.fmean(ys)
    sxx = sum((t - mt) ** 2 for t in ts); sxy = sum((t - mt) * (y - my) for t, y in zip(ts, ys))
    if sxx == 0: return 0.0, 0.0, 0.0
    b = sxy / sxx; a = my - b * mt
    ss_tot = sum((y - my) ** 2 for y in ys); ss_res = sum((y - (a + b * t)) ** 2 for t, y in zip(ts, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    start = a + b * ts[0]; rel = (b * (ts[-1] - ts[0])) / start if start > 0 else 0.0
    return b * 3600, r2, rel

def judge(rows, warmup=0.15):
    """rows: list of dicts with 't' (s since start) and numeric columns. Returns (verdicts, ok)."""
    if len(rows) < 12: return [("run too short to judge drift", "inconclusive")], None
    skip = int(len(rows) * warmup); rows = rows[skip:]
    ts = [r["t"] for r in rows]; out = []; ok = True; inconclusive = False
    span_h = (ts[-1] - ts[0]) / 3600
    for col, label, abs_min, rel_min in [(c, c, 0, 0) for c in rows[0] if c.startswith(("rss_", "fd_", "thr_"))] + [("queue_depth", "core queue depth", 0, 0), ("hist_db_mb", "historian size", None, None)]:
        if col not in rows[0]: continue
        ys = [r[col] for r in rows if r.get(col) is not None]
        if len(ys) < 8 or col == "hist_db_mb": continue
        sl, r2, rel = slope_per_hour(ts[: len(ys)], ys)
        unit = "MB/h" if col.startswith("rss_") else "/h"
        # growth that is steady (R^2 > 0.6), material (> 10 % of the starting level over the run) and not noise (> 0.5 unit/h) is a suspected leak
        leak = r2 > 0.6 and rel > 0.10 and sl > (0.5 if col.startswith(("rss_", "fd_", "thr_")) else 0.2)
        if leak and span_h < 2.0:      # JVM heap, caches and numpy pools all grow for the first hours; a short run cannot tell that from a leak
            out.append((f"{col}: {sl:+.2f} {unit} (R2 {r2:.2f}, {rel * 100:+.0f}% over {span_h:.2f} h)", "growing, inconclusive (needs >= 2 h after warm-up)")); inconclusive = True
            continue
        out.append((f"{col}: {sl:+.2f} {unit} (R2 {r2:.2f}, {rel * 100:+.0f}% over {span_h:.2f} h)", "LEAK SUSPECTED" if leak else "ok"))
        ok = ok and not leak
    for col in ("p99_ms",):
        ys = [r[col] for r in rows if r.get(col) is not None]
        if len(ys) >= 8:
            h = len(ys) // 2; a, b = statistics.median(ys[:h]), statistics.median(ys[h:])
            bad = b > 1.5 * max(a, 1.0) and b - a > 2.0
            out.append((f"core latency p99 median first half {a:.1f} ms -> second half {b:.1f} ms", "DEGRADED" if bad else "ok")); ok = ok and not bad
    last = rows[-1]
    for col, label in (("op_fail", "operator/analysis calls failed"), ("health_bad", "samples with an unhealthy service"), ("proc_missing", "samples with a missing process")):
        v = last.get(col, 0) or 0
        out.append((f"{label}: {v} of {len(rows)} samples" if col != "op_fail" else f"{label}: {v} (of {last.get('op_total', '?')})", "ok" if v == 0 else "FAILURES")); ok = ok and v == 0
    return out, (None if (ok and inconclusive) else ok)

def selftest():
    rnd = random.Random(1); fails = 0
    def series(leak_mb_h=0.0, noise=3.0, hours=24, n=288, drift_p99=1.0):
        rows = []
        for i in range(n):
            t = i * hours * 3600 / n
            rows.append({"t": t, "rss_coreA": 300 + leak_mb_h * t / 3600 + rnd.gauss(0, noise), "fd_coreA": 120 + rnd.randint(-2, 2), "thr_coreA": 60,
                         "queue_depth": rnd.randint(0, 5), "p99_ms": 8 * (1 + (drift_p99 - 1) * i / n) + rnd.gauss(0, 0.5), "op_fail": 0, "op_total": i, "health_bad": 0, "proc_missing": 0})
        return rows
    for name, rows, want in (("flat memory", series(), True), ("noisy flat memory", series(noise=15), True), ("leak 5 MB/h over 24 h", series(leak_mb_h=5), False),
                             ("small leak 0.2 MB/h (below threshold)", series(leak_mb_h=0.2), True), ("latency drifts to 4x", series(drift_p99=4), False), ("latency drifts 20% (noise level)", series(drift_p99=1.2), True)):
        v, ok = judge(rows); res = (ok is True) == want; fails += not res
        print(("PASS " if res else "FAIL ") + f"{name}: verdict {'ok' if ok else 'flagged'}" + ("" if res else "  " + str(v)))
    fd = series(); [r.update(fd_coreA=100 + int(r["t"] / 3600 * 2)) for r in fd]
    v, ok = judge(fd); res = ok is False; fails += not res; print(("PASS " if res else "FAIL ") + "fd leak (2/h) flagged")
    miss = series(); miss[-1]["proc_missing"] = 3
    v, ok = judge(miss); res = ok is False; fails += not res; print(("PASS " if res else "FAIL ") + "missing processes flagged")
    v, ok = judge(series(n=5)); res = ok is None; fails += not res; print(("PASS " if res else "FAIL ") + "short run reported inconclusive, not ok")
    sh = series(leak_mb_h=400, hours=0.5, n=100); v, ok = judge(sh); res = ok is None; fails += not res; print(("PASS " if res else "FAIL ") + "fast growth over 30 min is 'inconclusive', not a leak verdict")
    sh = series(leak_mb_h=400, hours=0.5, n=100); sh[-1]["proc_missing"] = 1; v, ok = judge(sh); res = ok is False; fails += not res; print(("PASS " if res else "FAIL ") + "hard failures are still failures on a short run")
    print(f"selftest: {'all passed' if not fails else str(fails) + ' failed'}"); return fails == 0

# ---------------------------------------------------------------- workload
class Workload:
    def __init__(self):
        from gs_client import Sess
        self.Sess = Sess; self.total = 0; self.fail = 0; self.errs = {}; self.lock = threading.Lock()
        self.login()
    def login(self):
        self.alice = self.Sess("alice"); self.bob = self.Sess("bob")
    def rec(self, name, ok_, detail=""):
        with self.lock:
            self.total += 1
            if not ok_:
                self.fail += 1; self.errs[name] = self.errs.get(name, 0) + 1
                if self.errs[name] <= 3: print(f"  op failed: {name} {str(detail)[:150]}", flush=True)
    def step(self, i):
        a = self.alice; t0 = time.time()
        try:
            r = a.get("/api/alarms"); self.rec("alarms", r.status_code == 200, r.status_code)
            r = a.get("/api/snapshot"); self.rec("snapshot", r.status_code == 200, r.status_code)
            if i % 4 == 0:
                r = a.post("/api/log", json={"category": "general", "text": f"soak note {i}"}); self.rec("logbook", r.status_code in (200, 201), r.status_code)
            if i % 6 == 0:
                r = httpx.post("http://127.0.0.1:8082/v1/powerflow", headers={"Authorization": "Bearer " + a.tok}, json={"model": "ieee14"}, timeout=60); self.rec("powerflow", r.status_code == 200, r.status_code)
            if i % 12 == 0:
                r = httpx.post("http://127.0.0.1:8082/v1/state-estimation", headers={"Authorization": "Bearer " + a.tok}, json={"model": "ieee14", "simulate": {"seed": i}}, timeout=60)
                self.rec("state-estimation", r.status_code in (200, 422), r.status_code)
            if i % 3 == 0:
                now = int(time.time() * 1000)
                r = httpx.get("http://127.0.0.1:8085/v1/trend", headers={"Authorization": "Bearer " + a.tok}, params={"tag": "G001.P", "from": now - 3600_000, "to": now}, timeout=20); self.rec("trend", r.status_code == 200, r.status_code)
            if i % 20 == 0:
                r = a.get("/api/metrics"); self.rec("metrics", r.status_code in (200, 403), r.status_code)
        except Exception as e:
            self.rec("exception", False, repr(e))
        return time.time() - t0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0); ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--interval", type=float, default=30); ap.add_argument("--out", default="")
    ap.add_argument("--analyze"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest: sys.exit(0 if selftest() else 1)
    if a.analyze:
        rows = [{k: (float(v) if v not in ("", None) else None) for k, v in r.items()} for r in csv.DictReader(open(os.path.join(a.analyze, "samples.csv")))]
        v, ok = judge(rows); [print(f"  [{s}] {m}") for m, s in v]; sys.exit(0 if ok in (True, None) else 2)
    dur = a.hours * 3600 + a.minutes * 60
    if dur <= 0: ap.error("give --hours or --minutes")
    out = a.out or f"/tmp/gs/soak/{time.strftime('%Y%m%d-%H%M%S')}"; os.makedirs(out, exist_ok=True)
    w = Workload(); t_start = time.time(); rows = []; i = 0; last_login = t_start
    cols = None; f = None
    print(f"soak: {dur / 3600:.2f} h, sample every {a.interval:.0f} s, output {out}", flush=True)
    while time.time() - t_start < dur:
        tick = time.time()
        if tick - last_login > 2400: w.login(); last_login = tick
        wl = w.step(i); i += 1
        row = {"t": round(tick - t_start, 1), "op_total": w.total, "op_fail": w.fail, "proc_missing": 0, "health_bad": 0}
        for n, pat in PROCS.items():
            pid = pid_of(pat); st = proc_stats(pid) if pid else None
            if st: row[f"rss_{n}"], row[f"fd_{n}"], row[f"thr_{n}"] = round(st[0], 1), st[1], st[2]
            elif n in ("coreA", "fep", "cimpds", "ems", "hist"): row["proc_missing"] += 1
        for n, u in (("core", "http://127.0.0.1:8083"), ("cim", "http://127.0.0.1:8081"), ("ems", "http://127.0.0.1:8082"), ("hist", "http://127.0.0.1:8085"), ("fep", "http://127.0.0.1:8084")):
            try:
                r = httpx.get(u + "/health", timeout=5)
                if r.status_code != 200 or r.json().get("status") not in ("ok",): row["health_bad"] += 1
            except Exception: row["health_bad"] += 1
        try:
            m = w.alice.get("/api/metrics").json() if False else httpx.get("http://127.0.0.1:8083/api/metrics", headers={"Authorization": "Bearer " + w.Sess("admin").tok}, timeout=10).json()
            row.update(queue_depth=m["queue_depth"], dropped=m["dropped"], p50_ms=m["latency_ms_p50"], p99_ms=m["latency_ms_p99"], points=m["points"], log_mb=round(m.get("log_bytes", 0) / 1048576, 2), alarms_active=m["alarms"]["active"])
        except Exception as e: row["metrics_err"] = type(e).__name__
        try:
            hs = httpx.get("http://127.0.0.1:8085/v1/stats", headers={"Authorization": "Bearer " + w.alice.tok}, timeout=10).json()
            row.update(hist_rows=hs["raw_rows"], hist_db_mb=round(hs["db_bytes"] / 1048576, 1))
        except Exception: pass
        st = os.statvfs("/tmp"); row["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 2)
        rows.append(row)
        if cols is None or set(row) - set(cols):
            cols = list(dict.fromkeys((cols or []) + list(row)))
        with open(os.path.join(out, "samples.csv"), "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=cols); wr.writeheader(); wr.writerows(rows)
        time.sleep(max(0.0, a.interval - (time.time() - tick)))
    v, ok = judge([{k: x for k, x in r.items()} for r in rows])
    summary = dict(duration_h=round((time.time() - t_start) / 3600, 3), samples=len(rows), ops=w.total, ops_failed=w.fail, failures_by_op=w.errs, ok=ok, verdicts=[dict(finding=m, status=s) for m, s in v])
    json.dump(summary, open(os.path.join(out, "summary.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1)); sys.exit(0 if ok in (True, None) else 2)

if __name__ == "__main__":
    main()
