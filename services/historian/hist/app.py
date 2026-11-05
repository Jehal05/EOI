"""Historian service: follows the live stream of a scada-core node (fails over to its peer) and serves trends."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, threading, time
import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from .store import Store
from . import forecast as fc

from . import asym_token as _asym
_asym.load_from_env()
SECRET = os.environ.get("GS_SECRET", "").encode()
if _asym.asymmetric():
    pass
elif len(SECRET) < 16:
    if os.environ.get("GS_DEV") == "1":
        SECRET = b"dev-secret-do-not-use-in-prod"
    else:
        raise SystemExit("GS_SECRET (>=16 chars) required; GS_DEV=1 for development only")
CORES = os.environ.get("HIST_CORES", "http://127.0.0.1:8083,http://127.0.0.1:8183").split(",")
RANK = {"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint(sub: str, role: str, ttl=600) -> str:
    if _asym.asymmetric():
        return _asym.mint(sub, role, ["*"], ttl)
    p = _b64(json.dumps({"sub": sub, "role": role, "jur": ["*"], "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    return p + "." + hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()


def require(min_role: str):
    def dep(authorization: str = Header(default="")):
        tok = authorization.removeprefix("Bearer ")
        try:
            if _asym.asymmetric():
                c = _asym.verify(tok)
                if RANK.get(c["role"], 0) < RANK[min_role]:
                    raise HTTPException(403, f"{min_role} role required")
                return c
            p, sig = tok.split(".", 1)
            if not hmac.compare_digest(hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest(), sig):
                raise ValueError
            c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
            if c["exp"] < time.time():
                raise ValueError
        except Exception:
            raise HTTPException(401, "invalid token")
        if RANK.get(c["role"], 0) < RANK[min_role]:
            raise HTTPException(403, f"{min_role} role required")
        return c
    return dep


_dsn = open(os.environ["HIST_DSN_FILE"]).read().strip() if os.environ.get("HIST_DSN_FILE") else os.environ.get("HIST_DSN")
if _dsn:
    from .pgstore import PgStore
    store = PgStore(_dsn)      # PostgreSQL / TimescaleDB (HIST_DSN_FILE: secret mounted as a file)
else:
    if not os.environ.get("HIST_DB") and os.environ.get("GS_DEV") != "1":
        raise SystemExit("HIST_DB (SQLite path) or HIST_DSN (PostgreSQL) is required; there is no default location outside GS_DEV=1")
    store = Store(os.environ.get("HIST_DB", "/tmp/gs/historian.db"))
app = FastAPI(title="GridSovereign Historian", version="0.1.0")
state = {"connected_to": None, "reconnects": 0, "batches": 0, "last_error": None}


def follow():
    """Consume NDJSON deltas from whichever core node answers; reconnect with backoff."""
    i = 0
    backoff = 0.5
    while True:
        url = CORES[i % len(CORES)]
        try:
            with httpx.stream("GET", url + "/api/stream", headers={"Authorization": "Bearer " + mint("historian", "viewer")}, timeout=httpx.Timeout(10, read=15)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"{url}: HTTP {r.status_code}")
                state["connected_to"] = url
                backoff = 0.5
                first = True
                for line in r.iter_lines():
                    if not line:
                        continue
                    m = json.loads(line)
                    pts = m.get("pts")
                    if pts:
                        # the first message is a full snapshot; keep it (gives every tag a starting row)
                        store.write(pts)
                        state["batches"] += 1
                    first = False
        except Exception as e:
            state["last_error"] = f"{type(e).__name__}: {e}"
        state["connected_to"] = None
        state["reconnects"] += 1
        i += 1
        time.sleep(backoff)
        backoff = min(backoff * 2, 5)


@app.on_event("startup")
def _start():
    if os.environ.get("HIST_BUS"):                  # telemetry from the message bus instead of following a core node
        from . import busconsumer
        threading.Thread(target=busconsumer.run, args=(store, state, os.environ["HIST_BUS"], os.environ.get("HIST_BUS_STREAM", "gs:telemetry"),
                                                       os.environ.get("HIST_BUS_GROUP", "historian")), daemon=True).start()
    else:
        threading.Thread(target=follow, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok" if state["connected_to"] else "degraded", "service": "historian", **state}


@app.get("/v1/trend")
def trend(tag: str = Query(max_length=128), t_from: int = Query(alias="from"), t_to: int = Query(alias="to"),
          max_points: int = Query(600, ge=10, le=5000), _=Depends(require("viewer"))):
    if t_to < t_from or t_to - t_from > 366 * 86400_000:
        raise HTTPException(400, "bad time range")
    t0 = time.perf_counter()
    r = store.trend(tag, t_from, t_to, max_points)
    r["query_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return r


@app.get("/v1/stats")
def stats(_=Depends(require("viewer"))):
    return store.stats()


CORE_URL = CORES[0]


class FcReq(BaseModel):
    tags: list[str] = Field(min_length=1, max_length=64)
    mode: str = Field(default="sum", pattern="^(sum|single)$")
    horizon_min: int = Field(default=60, ge=5, le=1440)
    step_min: int = Field(default=5, ge=1, le=60)
    history_hours: int = Field(default=72, ge=1, le=24 * 60)
    nonneg: bool = False
    cap: float | None = None
    kind: str = Field(default="load", pattern="^(load|solar|wind)$")


def _ai_gate(authorization: str, target: str, detail: str):
    """Ask the core for permission AND record the use in its audit chain in one call. Fails CLOSED on any problem."""
    for base in CORES:
        try:
            r = httpx.post(f"{base}/api/ai/use", headers={"Authorization": authorization}, timeout=3,
                           json={"feature": "forecast", "target": target[:128], "detail": detail[:200]})
        except Exception:
            continue
        if r.status_code == 409:
            raise HTTPException(503, "AI/ML advisory is switched off")
        if r.status_code == 200:
            return
        if r.status_code in (401, 403):
            raise HTTPException(r.status_code, "not permitted")
    raise HTTPException(503, "AI/ML advisory unavailable: the audit/permission service could not be reached (fail closed)")


@app.post("/v1/forecast")
def forecast_api(req: FcReq, authorization: str = Header(default=""), _=Depends(require("viewer"))):
    _ai_gate(authorization, ",".join(req.tags)[:128] if len(req.tags) < 4 else f"{req.tags[0]}+{len(req.tags) - 1} more",
             f"{req.kind} h={req.horizon_min}m step={req.step_min}m")
    now = int(time.time() * 1000)
    t_to = (store.stats().get("newest_ms") or now)
    t_from = t_to - req.history_hours * 3_600_000
    step = req.step_min * 60_000
    grids, vals = [], []
    for tag in req.tags:
        tt, vv = store.series(tag, t_from, t_to)
        if len(tt) < 2:
            continue
        g, y = fc.resample(tt, vv, step)
        grids.append(g); vals.append(y)
    if not grids:
        raise HTTPException(404, "no history for the requested tags")
    lo = max(g[0] for g in grids); hi = min(g[-1] for g in grids)
    if hi <= lo:
        raise HTTPException(422, "the requested tags have no overlapping history")
    cols = [y[(g >= lo) & (g <= hi)] for g, y in zip(grids, vals)]
    grid = np.arange(lo, hi + step, step, dtype=np.int64)[: len(cols[0])]
    y = np.nansum(np.vstack(cols), axis=0) if req.mode == "sum" else cols[0]
    try:
        r = fc.forecast(grid, y, step_min=req.step_min, horizon_min=req.horizon_min, nonneg=req.nonneg or req.kind in ("solar", "wind"), cap=req.cap)
    except fc.ForecastError as e:
        raise HTTPException(422, str(e))
    r.update(tags=req.tags, mode=req.mode, kind=req.kind, ai="advisory only; switchable off by an administrator; every use is audited")
    return r
