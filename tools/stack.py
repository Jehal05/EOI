#!/usr/bin/env python3
"""Portable start/stop for the local demo stack (Windows, Linux, macOS). Dev secrets; NOT for production.

    python tools/stack.py up        start everything (two SCADA core nodes, the default mode)
    python tools/stack.py down      stop everything
    python tools/stack.py status    health of every service
    python tools/stack.py trip      simulate the breaker trip used in the demo
    python tools/stack.py creds     print the demo logins file
    python tools/stack.py code alice   print alice's password and the current one-time login code

This starts the same processes as tools/stack.sh in its default mode. The optional modes of stack.sh
(TLS, DNP3, quorum, mTLS, PostgreSQL, Redis, PMU) are Linux-only and are not offered here.
Needs: Java 21+, and a Python with the packages in services/*/requirements.txt (the one running this script).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = os.name == "nt"
EXE = ".exe" if WIN else ""
D = os.environ.get("GS_DIR") or (os.path.join(os.environ.get("TEMP", "."), "gs") if WIN else "/tmp/gs")
MODEL = os.environ.get("GS_MODEL", "ieee118")
J = os.path.join
FEPDIR = J(ROOT, "services", "fep-gateway")
CIM = J(ROOT, "services", "cim-pds", "target", "release", "cim-pds" + EXE)
ENV = dict(os.environ, GS_DEV="1", CIM_PDS_DEV="1", EMS_DEV="1", CIM_DATA_DIR=J(ROOT, "data"), JAVA_TOOL_OPTIONS="")
NAMES = ["cim", "ems", "out", "field", "fep", "coreA", "coreB", "hist"]
PORTS = [8081, 8082, 8083, 8183, 8084, 8085, 8090, 8091]


def say(msg):
    print(msg, flush=True)


def pidfile(n):
    return J(D, n + ".pid")


def start(name, argv, cwd=None, env=None):
    log = open(J(D, name + ".log"), "wb")
    kw = {}
    if WIN:
        kw["creationflags"] = 0x00000200 | 0x08000000  # new process group, no console window
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(argv, cwd=cwd or ROOT, env=env or ENV, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **kw)
    with open(pidfile(name), "w") as f:
        f.write(str(p.pid))
    return p


def wait_http(url, tries=300):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.2)
    say("TIMEOUT waiting for " + url)
    return False


def kill(pid):
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass


def down():
    for n in NAMES:
        try:
            with open(pidfile(n)) as f:
                kill(int(f.read().strip()))
        except (OSError, ValueError):
            pass
        try:
            os.remove(pidfile(n))
        except OSError:
            pass
    time.sleep(1)


def java_major(java):
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, env=dict(os.environ, JAVA_TOOL_OPTIONS="")).stderr
        import re
        m = re.search(r'version "(\d+)', out)
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def find_java():
    """Java 21 or newer: JAVA_HOME, then the usual Windows install folders, then PATH."""
    cands = []
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(J(jh, "bin", "java" + EXE))
    if WIN:
        import glob
        for pat in (r"C:\Program Files\Eclipse Adoptium\jdk-*", r"C:\Program Files\Java\jdk-*", r"C:\Program Files\Microsoft\jdk-*",
                    r"C:\Program Files\Amazon Corretto\jdk*", r"C:\Program Files\Zulu\zulu*"):
            for d in sorted(glob.glob(pat), reverse=True):
                cands.append(J(d, "bin", "java.exe"))
    w = shutil.which("java")
    if w:
        cands.append(w)
    seen = []
    for c in cands:
        if os.path.exists(c) and c not in seen:
            seen.append(c)
            if java_major(c) >= 21:
                say("using Java at " + c)
                return c
    say("Java 21 or newer was not found. Found: " + (", ".join("%s (Java %d)" % (c, java_major(c)) for c in seen) or "none"))
    say("Install Temurin 21 from https://adoptium.net and run this again.")
    sys.exit(1)


def need(path, what):
    if not os.path.exists(path):
        say("missing %s: %s" % (what, path))
        sys.exit(1)


def up():
    os.makedirs(D, exist_ok=True)
    java = find_java()
    need(CIM, "model service (cim-pds)")
    need(J(FEPDIR, "bin", "fep" + EXE), "field gateway")
    need(J(FEPDIR, "bin", "outstation-sim" + EXE), "outstation simulator")
    need(J(ROOT, "services", "scada-core", "out"), "compiled SCADA core")
    down()
    shutil.rmtree(J(D, "state"), ignore_errors=True)
    py = sys.executable
    start("cim", [CIM], env=dict(ENV, CIM_STATE_DIR=J(D, "state")))
    start("ems", [py, "-m", "uvicorn", "ems.api:app", "--host", "127.0.0.1", "--port", "8082"],
          cwd=J(ROOT, "services", "ems-analytics"))
    if not (wait_http("http://127.0.0.1:8081/health") and wait_http("http://127.0.0.1:8082/health")):
        sys.exit(1)
    tok = subprocess.run([CIM, "mint", "outstation", "viewer", "*", "86400"], env=ENV, capture_output=True,
                         text=True).stdout.strip().splitlines()
    tok = tok[-1] if tok else ""
    start("out", [J(FEPDIR, "bin", "outstation-sim" + EXE), "-model", MODEL], cwd=FEPDIR, env=dict(ENV, CIM_TOKEN=tok))
    if not wait_http("http://127.0.0.1:8090/health"):
        sys.exit(1)
    start("field", [py, J(ROOT, "tools", "field_sim.py")], env=dict(ENV, GS_MODEL=MODEL))
    if not wait_http("http://127.0.0.1:8091/health"):
        sys.exit(1)
    core = [java, "-XX:+UseZGC", "-Xmx1g", "-cp", J(ROOT, "services", "scada-core", "out"), "gs.core.Main"]
    ui = ["-ui", J(ROOT, "ui", "dist")] if os.path.isdir(J(ROOT, "ui", "dist")) else []
    for n in ("coreA", "coreB"):
        shutil.rmtree(J(D, n), ignore_errors=True)
    start("coreA", core + ["-id", "A", "-listen", "127.0.0.1:8083", "-data", J(D, "coreA"), "-model", MODEL,
                            "-peer", "B=http://127.0.0.1:8183"] + ui)
    if not wait_http("http://127.0.0.1:8083/health"):
        say(open(J(D, "coreA.log"), errors="replace").read())
        sys.exit(1)
    os.makedirs(J(D, "coreB"), exist_ok=True)
    for f in ("users.json", "dev-credentials.txt"):
        shutil.copy(J(D, "coreA", f), J(D, "coreB", f))
    start("coreB", core + ["-id", "B", "-listen", "127.0.0.1:8183", "-data", J(D, "coreB"), "-model", MODEL,
                            "-peer", "A=http://127.0.0.1:8083"] + ui)
    if not wait_http("http://127.0.0.1:8183/health"):
        say(open(J(D, "coreB.log"), errors="replace").read())
        sys.exit(1)
    for f in ("historian.db", "historian.db-wal", "historian.db-shm"):
        try:
            os.remove(J(D, f))
        except OSError:
            pass
    hdb = J(D, "historian.db")
    start("hist", [py, "-m", "uvicorn", "hist.app:app", "--host", "127.0.0.1", "--port", "8085"],
          cwd=J(ROOT, "services", "historian"), env=dict(ENV, HIST_DB=hdb))
    wait_http("http://127.0.0.1:8085/health")
    subprocess.run([py, J(ROOT, "tools", "seed_history.py"), hdb], env=ENV,
                   stdout=open(J(D, "hist.log"), "ab"), stderr=subprocess.STDOUT)
    start("fep", [J(FEPDIR, "bin", "fep" + EXE), "-model", MODEL, "-ingest", "http://127.0.0.1:8083/api/ingest",
                  "-ingest", "http://127.0.0.1:8183/api/ingest"], cwd=FEPDIR)
    if not wait_http("http://127.0.0.1:8084/health"):
        sys.exit(1)
    say("stack up")
    status()
    say("")
    say("Open http://127.0.0.1:8083 in your browser. Logins: python tools/stack.py creds")


def status():
    bad = 0
    for p in PORTS:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % p, timeout=2) as r:
                body = r.read(90).decode("utf-8", "replace").strip()
            say("%d ok  %s" % (p, body))
        except Exception:
            say("%d DOWN" % p)
            bad += 1
    return bad


def trip():
    req = urllib.request.Request("http://127.0.0.1:8091/event", data=json.dumps(
        {"trip": ["BRK-BR0030-F", "BRK-BR0030-T"]}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        say(r.read().decode())


def creds():
    p = J(D, "coreA", "dev-credentials.txt")
    say(open(p, encoding="utf-8").read() if os.path.exists(p) else "not started yet: run  python tools/stack.py up")


def code(user):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gs_client
    c = gs_client.creds().get(user)
    if not c:
        say("unknown user " + user)
        sys.exit(1)
    say("user %s   password %s   one-time code %s   (the code changes every 30 seconds)" % (user, c["pw"], gs_client.totp(c["totp"])))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "up":
        up()
    elif cmd == "down":
        down()
        say("stack down")
    elif cmd == "status":
        sys.exit(1 if status() else 0)
    elif cmd == "trip":
        trip()
    elif cmd == "creds":
        creds()
    elif cmd == "code":
        code(sys.argv[2] if len(sys.argv) > 2 else "alice")
    else:
        say(__doc__)
        sys.exit(2)
