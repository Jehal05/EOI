#!/usr/bin/env python3
"""Runs every automated suite of the project, in the stack mode each one needs, and records pass/fail counts and timings in
docs/conformance_run.json (the evidence file that docs/CONFORMANCE.md and docs/TRACEABILITY.md refer to).
Takes about 45 minutes on the 2-core development host. Restarts the stack many times; do not use the stack meanwhile.
  tools/conformance.py [--only NAME,NAME] [--skip-unit]"""
import json, os, re, subprocess, sys, time, argparse, datetime
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = "/home/claude/venv/bin/python"; PY_UI = "/usr/local/bin/python3"
LOGS = "/tmp/gs/conf"; os.makedirs(LOGS, exist_ok=True)
ASYM = {"GS_DEV": "1", "EMS_DEV": "1", "GS_TOKEN_TRUST": "/tmp/gs/tokkeys/trust.json", "GS_TOKEN_KEY": "/tmp/gs/tokkeys/e2e.tokkey"}
HIST = {"HIST_TEST_DSN": "postgresql://postgres@127.0.0.1:5433/histtest", "HIST_TEST_REDIS": "redis://127.0.0.1:6390", "GS_DEV": "1"}

def rx(pats):
    def parse(out):
        for p in pats:
            m = re.findall(p, out)
            if m: return m[-1]
        return None
    return parse

UNIT = [  # name, cwd, command, regex (groups: passed, failed) or callable
    ("java-core", "services/scada-core", "./build.sh test", r"passed=(\d+) failed=(\d+)"),
    ("rust-cim-pds", "services/cim-pds", "cargo test --release", r"test result: \w+\. (\d+) passed; (\d+) failed"),
    ("go-fep", "services/fep-gateway", "go test -count=1 ./...", "go"),
    ("python-ems", "services/ems-analytics", f"EMS_DEV=1 {PY} -m pytest -q -x", r"(\d+) passed()"),
    ("python-historian", "services/historian", f"{PY} -m pytest -q", r"(\d+) passed()"),
    ("ui-unit", "ui", "npm test", r"Tests\s+(\d+) passed \((\d+)\)"),
    ("ui-typecheck", "ui", "npx tsc --noEmit && echo OK=1", r"OK=(1)()"),
    ("soak-selftest", ".", f"{PY} tools/soak.py --selftest", r"selftest: (all) passed()"),
    ("usability-selftest", ".", f"{PY} tools/usability.py --selftest", r"selftest: (all) passed()"),
    ("sast", ".", "tools/sast.sh", r"SAST: (no) blocking findings()"),
    ("sbom", ".", f"{PY} tools/sbom.py", r"(\d+) components; policy fail (\d+)"),
]
# (stack environment, [(name, command, env, interpreter)])
STACKS = [
    ("plain", {}, [("e2e_core", "tools/e2e_core.py", {}, PY), ("e2e_models", "tools/e2e_models.py", {}, PY), ("e2e_interop", "tools/e2e_interop.py", {}, PY),
                   ("e2e_distributed", "tools/e2e_distributed.py", {}, PY), ("dast", "tools/dast.py", {}, PY), ("ui_e2e", "tools/ui_e2e.py", {}, PY_UI),
                   ("e2e_comms", "tools/e2e_comms.py", {}, PY), ("e2e_failover", "tools/e2e_failover.py", {}, PY)]),
    ("secure-links", "SELF", [("e2e_secure", "tools/e2e_secure.py", {}, PY)]),
    ("asym+quorum+mtls", {"GS_ASYM": "1", "GS_QUORUM": "1", "GS_MTLS": "1"}, [("e2e_asym", "tools/e2e_asym.py", ASYM, PY), ("e2e_core_asym", "tools/e2e_core.py", ASYM, PY), ("e2e_mtls", "tools/e2e_mtls.py", ASYM, PY)]),
    ("asym+quorum", {"GS_ASYM": "1", "GS_QUORUM": "1"}, [("e2e_quorum", "tools/e2e_quorum.py", ASYM, PY)]),
    ("opcua", {"GS_OPCUA": "1"}, [("e2e_opcua", "tools/e2e_opcua.py", {}, PY)]),
    ("pmu", {"GS_PMU": "1"}, [("e2e_pmu", "tools/e2e_pmu.py", {}, PY)]),
    ("postgres+bus", {"GS_PG": "1", "GS_BUS": "1"}, [("e2e_bus", "tools/e2e_bus.py", {}, PY), ("ui_e2e_pg", "tools/ui_e2e.py", {}, PY_UI)]),
]

def run(cmd, cwd, env=None, timeout=3000):
    e = {**os.environ, **(env or {})}
    t = time.time()
    p = subprocess.run(cmd, shell=True, cwd=os.path.join(ROOT, cwd), env=e, capture_output=True, text=True, timeout=timeout, executable="/bin/bash")
    return p.returncode, p.stdout + p.stderr, round(time.time() - t, 1)

def counts(out, spec):
    if spec == "go":
        ok = len(re.findall(r"^ok\s", out, re.M)); bad = len(re.findall(r"^(FAIL|---\s+FAIL)", out, re.M)); return f"{ok} packages", bad
    m = re.findall(spec, out)
    if not m: return None, None
    a, b = m[-1]
    return a, (int(b) if b.isdigit() else 0) if spec != r"Tests\s+(\d+) passed \((\d+)\)" else 0

def stack_up(env):
    subprocess.run(f"{ROOT}/tools/stack.sh down", shell=True, capture_output=True)
    e = {**os.environ, **env}
    subprocess.Popen(f"setsid {ROOT}/tools/stack.sh up > /tmp/gs/up.out 2>&1", shell=True, env=e)
    for _ in range(150):
        r = subprocess.run("curl -sf -m2 localhost:8084/health >/dev/null", shell=True)
        if r.returncode == 0: break
        time.sleep(1)
    time.sleep(24)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--only", default=""); ap.add_argument("--skip-unit", action="store_true"); a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    res = {"started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "host": os.uname().nodename, "cpus": os.cpu_count(), "suites": []}
    out_path = os.path.join(ROOT, "docs", "conformance_run.json")
    ran = []
    prev = json.load(open(out_path)) if (only or a.skip_unit) and os.path.exists(out_path) else None
    def rec(group, name, rc, out, secs, spec):
        n, bad = counts(out, spec) if spec else counts(out, r"(\d+) passed, (\d+) failed")
        if spec is None and n is None: n, bad = counts(out, r"RESULT: (\d+) passed, (\d+) failed")
        if spec is None and n is None: n, bad = counts(out, r"(?:asym|mtls|bus|dast): (\d+) (?:passed|checks passed), (\d+) (?:failed|findings)")
        if spec is None and n is None: n, bad = counts(out, r"(\d+) checks passed, (\d+) findings")
        ok = rc == 0 and (bad in (0, None)) and n is not None
        ran.append(dict(group=group, name=name, passed=n, failed=bad, exit=rc, seconds=secs, ok=ok))
        # partial re-run: keep the other suites' last results, replace the ones just run
        res["suites"] = ([x for x in prev["suites"] if x["name"] not in {y["name"] for y in ran}] if prev else []) + ran
        print(f"{'PASS' if ok else 'FAIL'}  {group:18s} {name:18s} passed={n} failed={bad} exit={rc} {secs}s", flush=True)
        open(out_path, "w").write(json.dumps(res, indent=1))
    if not a.skip_unit:
        for name, cwd, cmd, spec in UNIT:
            if only and name not in only: continue
            rc, out, secs = run(cmd, cwd, HIST if name == "python-historian" else None, 1800); open(f"{LOGS}/{name}.log", "w").write(out)
            if name == "sbom": n, bad = (re.findall(spec, out) or [(None, None)])[-1]; rec("unit", name, rc, out, secs, spec)
            else: rec("unit", name, rc, out, secs, spec)
    for gname, senv, suites in STACKS:
        if only and not any(s[0] in only for s in suites): continue
        if senv != "SELF": stack_up(senv)
        first = True
        for name, script, env, interp in suites:
            if only and name not in only: continue
            if not first and senv != "SELF": stack_up(senv)      # every suite starts on a fresh stack: no state leaks between suites
            first = False
            rc, out, secs = run(f"{interp} {script}", ".", env, 3000); open(f"{LOGS}/{name}.log", "w").write(out)
            rec(gname, name, rc, out, secs, None)
    subprocess.run(f"{ROOT}/tools/stack.sh down", shell=True, capture_output=True)
    res["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if prev: res["started"], res["host"] = prev["started"], prev["host"]
    res["all_ok"] = all(s["ok"] for s in res["suites"]); open(out_path, "w").write(json.dumps(res, indent=1))
    print("ALL OK" if res["all_ok"] else "SOME SUITES FAILED"); sys.exit(0 if res["all_ok"] else 1)

if __name__ == "__main__":
    main()
