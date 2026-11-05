"""EMS analytics HTTP service. Reads the network model ONLY from cim-pds (CIM-first)."""
from __future__ import annotations
import os, re
import time
import httpx
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from . import auth, distributed as dist, contingency as ca, n2, opf as opfm, security as secm, agc as agcm, dsa as dsam, outage as outm, shed as shedm, dts as dtsm, tcm as tcmm, powerflow as pf, shortcircuit as scc, state_estimator as se
from .network import Net

app = FastAPI(title="GridSovereign EMS Analytics", version="0.1.0")
CIM_URL = os.environ.get("CIM_URL", "http://127.0.0.1:8081")


_PATH_OK = re.compile(r"/[A-Za-z0-9_./-]{1,200}")


def _cim_get(path: str, params=None):
    if not _PATH_OK.fullmatch(path) or ".." in path:        # model ids come from request bodies: never let them shape the upstream URL
        raise HTTPException(422, "invalid model identifier")
    tok = os.environ.get("EMS_CIM_TOKEN") or auth.mint("ems-analytics", "viewer", ["*"], 300)
    try:
        r = httpx.get(CIM_URL + path, params=params, headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise HTTPException(502, f"cim-pds unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(502 if r.status_code >= 500 else r.status_code, f"cim-pds: {r.text[:200]}")
    return r.json()


def load_net(model: str, version: int | None, open_sw: list[str] | None = None, close_sw: list[str] | None = None):
    params = {}
    if version:
        params["version"] = version
    if open_sw:
        params["open"] = ",".join(open_sw)
    if close_sw:
        params["close"] = ",".join(close_sw)
    d = _cim_get(f"/models/{model}/busbranch", params or None)
    return Net.from_busbranch(d), d["version"]


def _f(a, nd=5):
    return [round(float(x), nd) for x in a]


class PFReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)  # real-time SCADA overlay
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    outages: list[str] = Field(default_factory=list, max_length=50)
    enforce_q_limits: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "service": "ems-analytics"}


@app.post("/v1/powerflow")
def powerflow(req: PFReq, _=Depends(auth.require("viewer"))):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    if req.outages:
        idx = []
        for o in req.outages:
            if o not in net.br_id:
                raise HTTPException(404, f"branch {o} not in service in model version {ver}")
            idx.append(net.br_id.index(o))
        net = net.without_branches(idx)
    t0 = time.perf_counter()
    r = pf.solve(net, enforce_q_limits=req.enforce_q_limits)
    ms = (time.perf_counter() - t0) * 1000
    return dict(
        model=req.model, version=ver, converged=r["converged"], iterations=r["iterations"], solve_ms=round(ms, 2), strategy=r.get("strategy", "newton"),
        losses_mw=round(r["losses_mw"], 4), lost_load_mw=round(r["lost_load_mw"], 2), dead_buses=r["dead_buses"],
        buses=[dict(name=net.bus_name[i], vm=round(float(r["vm"][i]), 5), va_deg=round(float(r["va_deg"][i]), 4)) for i in range(net.nb)],
        branches=[dict(id=net.br_id[k], from_bus=net.bus_name[net.f[k]], to_bus=net.bus_name[net.t[k]],
                       pf=round(float(r["pf"][k]), 3), qf=round(float(r["qf"][k]), 3),
                       loading_pct=round(float(r["loading_pct"][k]), 1)) for k in range(net.nl)],
        q_limited_buses=r["q_limited_buses"],
    )


class MeasIn(BaseModel):
    kind: str = Field(pattern="^(vm|pinj|qinj|pf|qf|pt|qt)$")
    ref: str            # bus name (vm/pinj/qinj) or branch mRID (pf/qf/pt/qt)
    value: float
    sigma: float = Field(gt=0)


class Sim(BaseModel):
    seed: int = 1
    gross_errors: dict[str, float] = Field(default_factory=dict)


class SEReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)  # real-time SCADA overlay
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    measurements: list[MeasIn] | None = Field(default=None, max_length=200000)
    simulate: Sim | None = None
    bad_data: bool = True


@app.post("/v1/state-estimation")
def state_estimation(req: SEReq, _=Depends(auth.require("viewer"))):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    if (req.measurements is None) == (req.simulate is None):
        raise HTTPException(400, "provide exactly one of 'measurements' or 'simulate'")
    truth = None
    if req.simulate is not None:
        sol = pf.solve(net)
        ms = se.simulate_measurements(net, sol, np.random.default_rng(req.simulate.seed), gross=req.simulate.gross_errors)
        truth = sol
    else:
        bus_ix = {n: i for i, n in enumerate(net.bus_name)}
        br_ix = {n: i for i, n in enumerate(net.br_id)}
        ms = []
        for m in req.measurements:
            tab = bus_ix if m.kind in ("vm", "pinj", "qinj") else br_ix
            if m.ref not in tab:
                raise HTTPException(404, f"unknown element '{m.ref}' for kind {m.kind}")
            ms.append(se.Meas(m.kind, tab[m.ref], m.value, m.sigma, f"{m.kind.upper()}:{m.ref}"))
    t0 = time.perf_counter()
    e = se.estimate_network(net, ms, bad_data=req.bad_data)
    ms_t = (time.perf_counter() - t0) * 1000
    if not e["observable"]:
        return dict(model=req.model, version=ver, observable=False, reason=e.get("reason"))
    out = dict(model=req.model, version=ver, observable=True, converged=e["converged"], iterations=e["iterations"],
               solve_ms=round(ms_t, 1), J=round(e["J"], 2), dof=e["dof"], chi2_threshold=round(e["chi2_threshold"], 2),
               bad_data_detected=e["bad_data_detected"], rn_method=e.get("rn_method"), removed=e["removed"], top_residuals=e["top_residuals"],
               n_measurements=e["n_meas"], dead_buses=e.get("dead_buses", []),
               buses=[dict(name=net.bus_name[i], vm=round(float(e["vm"][i]), 5), va_deg=round(float(e["va_deg"][i]), 4)) for i in range(net.nb)])
    if truth is not None:
        ref = truth["va_deg"] - truth["va_deg"][net.slack_bus]
        out["rms_vm_error"] = float(np.sqrt(np.mean((e["vm"] - truth["vm"]) ** 2)))
        out["rms_angle_error_deg"] = float(np.sqrt(np.mean((e["va_deg"] - ref) ** 2)))
    return out


class CAReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)  # real-time SCADA overlay
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    workers: int = Field(default=2, ge=1, le=16)
    vmin: float = 0.95
    vmax: float = 1.05
    top: int = Field(default=25, ge=1, le=500)
    enforce_q_limits: bool = False


@app.post("/v1/contingency")
def contingency(req: CAReq, _=Depends(auth.require("viewer"))):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    r = ca.run_n1(net, workers=req.workers, vmin=req.vmin, vmax=req.vmax, enforce_q_limits=req.enforce_q_limits)
    r["results"] = r["results"][: req.top]
    r.update(model=req.model, version=ver, wall_seconds=round(r["wall_seconds"], 3), mean_case_ms=round(r["mean_case_ms"], 2))
    return r


class N2Req(CAReq):
    k_top: int = Field(default=40, ge=2, le=200)
    verify: int = Field(default=60, ge=1, le=500)
    top: int = Field(default=25, ge=1, le=500)


@app.post("/v1/contingency-n2")
def contingency_n2(req: N2Req, _=Depends(auth.require("viewer"))):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    r = n2.run_n2(net, workers=req.workers, vmin=req.vmin, vmax=req.vmax, k_top=req.k_top, verify=req.verify,
                  enforce_q_limits=req.enforce_q_limits)
    r["results"] = [{k: v for k, v in x.items() if k != "rank_key"} for x in r["results"][: req.top]]
    r.update(model=req.model, version=ver)
    return r


class SCReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    buses: list[str] | None = Field(default=None, max_length=20000)
    c: float = Field(default=1.10, ge=0.9, le=1.2)             # 1.10 maximum / 0.95 minimum fault current
    top: int = Field(default=200, ge=1, le=20000)


class SCDetailReq(SCReq):
    bus: str
    kind: str = Field(default="3ph", pattern="^(3ph|lg|ll|llg)$")


def _bus_index(net, names):
    ix = {n: i for i, n in enumerate(net.bus_name)}
    bad = [n for n in names if n not in ix]
    if bad:
        raise HTTPException(404, f"unknown bus {bad[0]}")
    return [ix[n] for n in names]


@app.post("/v1/short-circuit")
def short_circuit(req: SCReq, _=Depends(auth.require("viewer"))):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    sel = _bus_index(net, req.buses) if req.buses else None
    if sel is None and net.nb > 3000:
        raise HTTPException(413, "more than 3000 buses: pass an explicit bus list")
    r = scc.analyze(net, sel, c=req.c)
    r["results"] = r["results"][: req.top]
    r.update(model=req.model, version=ver)
    return r


@app.post("/v1/short-circuit/detail")
def short_circuit_detail(req: SCDetailReq, _=Depends(auth.require("viewer"))):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    try:
        return scc.fault_detail(net, _bus_index(net, [req.bus])[0], req.kind, c=req.c)
    except ValueError as e:
        raise HTTPException(422, str(e))


class OPFReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    mode: str = Field(default="opf", pattern="^(opf|scopf)$")
    emergency: float = Field(default=1.15, ge=1.0, le=2.0)       # post-outage limit as a multiple of the normal rating
    verify: bool = True
    vmin: float = 0.95
    vmax: float = 1.05
    segments: int = Field(default=16, ge=4, le=64)


@app.post("/v1/opf")
def opf_route(req: OPFReq, _=Depends(auth.require("operator"))):
    """Advisory dispatch study. Operator role or higher; it never changes any set-point."""
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    if req.mode == "scopf":
        r = opfm.run_scopf(net, emergency=req.emergency, verify=req.verify, vmin=req.vmin, vmax=req.vmax, segments=req.segments)
    else:
        r = opfm.run_opf(net, verify=req.verify, vmin=req.vmin, vmax=req.vmax, segments=req.segments)
    if not r.get("ok"):
        raise HTTPException(422, f"dispatch problem not solved: {r.get('message')}")
    r.update(model=req.model, version=ver)
    return r


class SecReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    gen_p: dict[str, float] = Field(default_factory=dict, max_length=5000)     # live MW by generator id
    load_p: dict[str, float] = Field(default_factory=dict, max_length=20000)   # live MW by load id
    emergency: float = Field(default=1.15, ge=1.0, le=2.0)
    use_switching: bool = True
    use_redispatch: bool = True
    use_shed: bool = True
    max_move_mw: float | None = Field(default=None, ge=0.0, le=10000.0)
    max_switching: int = Field(default=3, ge=0, le=10)
    rounds: int = Field(default=5, ge=1, le=8)


@app.post("/v1/security-plan")
def security_plan(req: SecReq, _=Depends(auth.require("operator"))):
    """Remedial-action PLAN for N-1 overloads. Advisory: returns proposals, executes nothing."""
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    net, used = secm.apply_overrides(net, req.gen_p, req.load_p)
    r = secm.plan(net, emergency=req.emergency, use_switching=req.use_switching, use_redispatch=req.use_redispatch,
                  use_shed=req.use_shed, max_move_mw=req.max_move_mw, max_switching=req.max_switching, rounds=req.rounds)
    r.update(model=req.model, version=ver, live_overrides=used)
    return r


class TcmReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list)
    close_switches: list[str] = Field(default_factory=list)
    gen_p: dict[str, float] = Field(default_factory=dict)
    load_p: dict[str, float] = Field(default_factory=dict)
    parts: int = Field(default=3, ge=2, le=6)
    trm_pct: float = Field(default=5.0, ge=0, le=50)
    n1_limit: float = Field(default=1.0, ge=0.8, le=1.6)
    ac_n1_top: int = Field(default=12, ge=0, le=60)


@app.post("/v1/tcm")
def tcm_monitor(req: TcmReq, _=Depends(auth.require("viewer"))):
    """Transfer capability / margin of inter-area corridors on the present operating point. Advisory."""
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    net, used = secm.apply_overrides(net, req.gen_p, req.load_p)
    r = tcmm.analyze(net, parts=req.parts, trm_pct=req.trm_pct, n1_limit=req.n1_limit, ac_n1_top=req.ac_n1_top)
    r.update(model=req.model, version=ver, live_overrides=used)
    return r


class AgcEvent(BaseModel):
    kind: str = Field(pattern="^(load_step|load_ramp|gen_trip)$")
    t: float = Field(default=5.0, ge=0, le=3600)
    area: int = Field(default=0, ge=0, le=9)
    mw: float = Field(default=0.0, ge=-5000, le=5000)
    duration: float = Field(default=60.0, gt=0, le=3600)
    unit: str | None = None


class AgcReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list)
    close_switches: list[str] = Field(default_factory=list)
    gen_p: dict[str, float] = Field(default_factory=dict)
    load_p: dict[str, float] = Field(default_factory=dict)
    parts: int = Field(default=2, ge=2, le=6)
    events: list[AgcEvent] = Field(default_factory=list, max_length=20)
    duration: float = Field(default=300.0, ge=10, le=1800)
    compare: bool = True
    params: dict[str, float | str] = Field(default_factory=dict)


@app.post("/v1/agc")
def agc_sim(req: AgcReq, _=Depends(auth.require("viewer"))):
    """Multi-area AGC SIMULATION with and without AGC (primary response only). Sends nothing to any unit."""
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    net, used = secm.apply_overrides(net, req.gen_p, req.load_p)
    allowed = set(agcm.DEFAULTS)
    params = {k: v for k, v in req.params.items() if k in allowed}
    setup = agcm.Setup(net, req.parts, params)
    ev = [e.model_dump() for e in req.events] or [dict(kind="load_step", t=5.0, area=0, mw=round(0.02 * float(setup.load[0]), 1), duration=60.0, unit=None)]
    for e in ev:
        if e["kind"] != "gen_trip" and e["area"] >= setup.na:
            raise HTTPException(400, f"area {e['area']} does not exist (model has {setup.na})")
        if e["kind"] == "gen_trip" and e["unit"] is None:          # default: the largest-output unit of the chosen area
            if e["area"] >= setup.na or not (setup.g_area == e["area"]).any():
                raise HTTPException(400, "area has no generating unit")
            cand = np.where(setup.g_area == e["area"])[0]
            e["unit"] = setup.ids[int(cand[np.argmax(setup.p0[cand])])]
        if e["kind"] == "gen_trip" and e["unit"] not in setup.ids:
            raise HTTPException(400, "unknown generator unit")
    r = agcm.simulate(net, parts=req.parts, events=ev, duration=req.duration, agc=True, params=params, setup=setup)
    out = dict(model=req.model, version=ver, live_overrides=used, events=ev, agc=r)
    if req.compare:
        out["primary_only"] = agcm.simulate(net, parts=req.parts, events=ev, duration=req.duration, agc=False, params=params, setup=setup)
    out["units"] = setup.ids
    return out


class DsaReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list)
    close_switches: list[str] = Field(default_factory=list)
    gen_p: dict[str, float] = Field(default_factory=dict)
    load_p: dict[str, float] = Field(default_factory=dict)
    n1_top: int = Field(default=6, ge=0, le=20)                 # voltage: outages assessed
    fault_buses: list[int] | None = None                        # transient: bus indices (default: first max_buses live buses)
    max_buses: int = Field(default=150, ge=1, le=400)
    clearing_time: float = Field(default=0.12, gt=0.0, le=1.0)
    trip_branch: bool = True
    cct_top: int = Field(default=6, ge=0, le=20)
    traj_bus: int | None = None
    params: dict[str, float] = Field(default_factory=dict)      # H, xd1, D, t_end, dt (synthetic defaults)


def _dsa_net(req):
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    net, used = secm.apply_overrides(net, req.gen_p, req.load_p)
    return net, ver, used


def _dsa_params(req):
    return {k: v for k, v in req.params.items() if k in dsam.DYN}


@app.post("/v1/dsa/voltage")
def dsa_voltage(req: DsaReq, _=Depends(auth.require("viewer"))):
    net, ver, used = _dsa_net(req)
    r = dsam.vsa(net, n1_top=req.n1_top)
    r.update(model=req.model, version=ver, live_overrides=used)
    return r


@app.post("/v1/dsa/transient")
def dsa_transient(req: DsaReq, _=Depends(auth.require("viewer"))):
    net, ver, used = _dsa_net(req)
    try:
        r = dsam.tsa(net, fault_buses=req.fault_buses, t_clear=req.clearing_time, trip_branch=req.trip_branch, cct_top=req.cct_top,
                     params=_dsa_params(req), max_buses=req.max_buses, traj_bus=req.traj_bus)
    except ValueError as e:
        raise HTTPException(422, str(e))
    r.update(model=req.model, version=ver, live_overrides=used)
    return r


@app.post("/v1/dsa/smallsignal")
def dsa_smallsignal(req: DsaReq, _=Depends(auth.require("viewer"))):
    net, ver, used = _dsa_net(req)
    try:
        r = dsam.ssa(net, params=_dsa_params(req))
    except ValueError as e:
        raise HTTPException(422, str(e))
    r.update(model=req.model, version=ver, live_overrides=used)
    return r


class OutReq(BaseModel):
    model: str
    version: int | None = None
    equipment: list[str] = Field(default_factory=list, max_length=10)
    concurrent: list[list[str]] = Field(default_factory=list, max_length=20)
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    mode: str = Field(default="isolate", pattern="^(isolate|restore)$")


class OutWindow(BaseModel):
    id: int
    equipment: list[str] = Field(min_length=1, max_length=10)
    start: int
    end: int


class OutSchedReq(BaseModel):
    model: str
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    requests: list[OutWindow] = Field(default_factory=list, max_length=30)
    # suggestion mode
    equipment: list[str] = Field(default_factory=list, max_length=10)
    duration_ms: int = Field(default=0, ge=0, le=30 * 86_400_000)
    earliest: int = 0
    latest: int = 0
    step_ms: int = Field(default=3_600_000, ge=900_000)


_CIM: dict = {}


def _cim(model: str):
    d = _cim_get(f"/models/{model}")
    key = (model, d.get("version"))
    if key not in _CIM:
        _CIM.clear()
        _CIM[key] = outm.Cim(d)
        outm.clear_cache()
    return _CIM[key]


def _loader(model: str, version):
    def f(open_list):
        net, _ = load_net(model, version, open_list, [])
        return net
    return f


@app.post("/v1/outage-study")
def outage_study(req: OutReq, _=Depends(auth.require("viewer"))):
    if not req.equipment:
        raise HTTPException(400, "equipment required")
    cim = _cim(req.model)
    r = outm.study(cim, _loader(req.model, req.version), req.equipment, concurrent=req.concurrent, live_open=req.open_switches)
    r.update(model=req.model)
    return r


@app.post("/v1/switching-plan")
def switching_plan(req: OutReq, _=Depends(auth.require("viewer"))):
    if not req.equipment:
        raise HTTPException(400, "equipment required")
    cim = _cim(req.model)
    r = outm.plan(cim, _loader(req.model, req.version), req.equipment, req.mode, live_open=req.open_switches)
    r.update(model=req.model, advisory=True)
    return r


@app.post("/v1/outage-schedule")
def outage_schedule(req: OutSchedReq, _=Depends(auth.require("viewer"))):
    cim = _cim(req.model)
    ld = _loader(req.model, None)
    if req.equipment and req.duration_ms:
        existing = [dict(equipment=w.equipment, start=w.start, end=w.end) for w in req.requests]
        return outm.suggest(cim, ld, req.equipment, req.duration_ms, req.earliest, req.latest or req.earliest + 14 * 86_400_000, existing, step_ms=req.step_ms, live_open=req.open_switches)
    return outm.schedule(cim, ld, [w.model_dump() for w in req.requests], live_open=req.open_switches)


@app.on_event("startup")
def _warmup():
    """Trigger numba/BLAS/scipy first-call costs in the background so the first operator-facing cycle is fast."""
    import threading

    def go():
        t0 = time.perf_counter()
        for _ in range(30):                       # cim-pds may still be starting
            try:
                net, _v = load_net("ieee14", None)
                sol = pf.solve(net)
                ms = se.simulate_measurements(net, sol, np.random.default_rng(1))
                se.estimate(net, ms)
                ca.run_n1(net, workers=1)
                print(f"warm-up complete in {time.perf_counter() - t0:.1f}s", flush=True)
                return
            except Exception:
                time.sleep(1)
    threading.Thread(target=go, daemon=True).start()


class ShedStage(BaseModel):
    f: float = Field(ge=46.0, le=50.0)
    delay: float = Field(default=0.2, ge=0.0, le=5.0)
    frac: float = Field(gt=0.0, le=0.5)


class ShedScenario(BaseModel):
    name: str = Field(default="scenario", max_length=60)
    largest: int | None = Field(default=None, ge=1, le=10)
    pct: float | None = Field(default=None, gt=0, le=60)


class ShedReq(BaseModel):
    model: str
    version: int | None = None
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    gen_p: dict[str, float] = Field(default_factory=dict)
    load_p: dict[str, float] = Field(default_factory=dict)
    overrides: dict[str, int] = Field(default_factory=dict, max_length=5000)        # load mrid -> priority class 1..4 (utility data)


class UflsReq(ShedReq):
    scenarios: list[ShedScenario] = Field(default_factory=list, max_length=10)
    stages: list[ShedStage] = Field(default_factory=list, max_length=8)
    params: dict[str, float] = Field(default_factory=dict)
    nadir_min: float = Field(default=48.8, ge=46, le=50)
    final_min: float = Field(default=49.5, ge=47, le=50)
    duration: float = Field(default=60.0, ge=10, le=300)


class RosterReq(ShedReq):
    deficit_mw: float = Field(gt=0, le=100000)
    hours: int = Field(default=12, ge=1, le=72)
    period_h: float = Field(default=1.0, ge=0.25, le=6)
    min_class: int = Field(default=3, ge=1, le=4)
    compare: bool = True


def _shed_net(req):
    net, ver = load_net(req.model, req.version, req.open_switches, [])
    net, used = secm.apply_overrides(net, req.gen_p, req.load_p)
    return net, ver, used


@app.post("/v1/shed/priorities")
def shed_priorities(req: ShedReq, _=Depends(auth.require("viewer"))):
    """Load priority classes (synthetic placeholder unless the caller supplies the utility's own)."""
    net, ver, used = _shed_net(req)
    return dict(model=req.model, version=ver, **shedm.priority_table(net, req.overrides))


@app.post("/v1/shed/ufls")
def shed_ufls(req: UflsReq, _=Depends(auth.require("viewer"))):
    """Under-frequency load shedding ASSESSMENT (simulation). Nothing is sent to any relay or breaker."""
    net, ver, used = _shed_net(req)
    allowed = set(shedm.DEFAULTS)
    sc = []
    for s_ in req.scenarios:
        if s_.largest is None and s_.pct is None:
            raise HTTPException(400, "each scenario needs 'largest' or 'pct'")
        sc.append(dict(name=s_.name, **({"largest": s_.largest} if s_.largest else {"pct": s_.pct})))
    stages = [x.model_dump() for x in req.stages] or None
    if stages and sum(x["frac"] for x in stages) > 0.6:
        raise HTTPException(400, "stages shed more than 60 % of the load in total")
    r = shedm.ufls(net, scenarios=sc or None, stages=stages, params={k: v for k, v in req.params.items() if k in allowed},
                   nadir_min=req.nadir_min, final_min=req.final_min, duration=req.duration)
    return dict(model=req.model, version=ver, live_overrides=used, **r)


@app.post("/v1/shed/roster")
def shed_roster(req: RosterReq, _=Depends(auth.require("viewer"))):
    """Rotating load-shed roster for a generation deficit, AC-checked per period. ADVISORY: nothing is switched."""
    net, ver, used = _shed_net(req)
    kw = dict(deficit_mw=req.deficit_mw, hours=req.hours, period_h=req.period_h, overrides=req.overrides or None, min_class=req.min_class)
    out = dict(model=req.model, version=ver, live_overrides=used, roster=shedm.roster(net, rotate=True, **kw))
    if req.compare:
        b = shedm.roster(net, rotate=False, **kw)
        out["static"] = dict(loads_ever_shed=b["loads_ever_shed"], jain_fairness=b["jain_fairness"], max_ens_mwh=b["max_ens_mwh"], total_ens_mwh=b["total_ens_mwh"])
    return out


# ------------------------------------------------------------------ dispatcher training simulator (private sandbox; never touches live data)
_DTS_LIB: dict = {}
_DTS_CUSTOM: dict[str, dict] = {}


def _dts_net(model: str):
    net, ver = load_net(model, None, None, None)          # the model as built, deliberately WITHOUT the live switch overlay
    key = (model, ver)
    if key not in _DTS_LIB:
        _DTS_LIB[key] = dtsm.library(net)
    return net, ver, _DTS_LIB[key]


class DtsScenario(BaseModel):
    model: str
    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(max_length=60)
    description: str = Field(default="", max_length=300)
    duration: float = Field(default=600, ge=30, le=7200)
    events: list[dict] = Field(max_length=50)
    stuck: list[str] = Field(default_factory=list, max_length=20)


class DtsStart(BaseModel):
    model: str
    scenario: str = Field(max_length=40)


class DtsStep(BaseModel):
    dt: float = Field(ge=1, le=600)


class DtsAct(BaseModel):
    action: str = Field(pattern=r"^(open_branch|close_branch|set_gen|shed_load)$")
    target: str = Field(min_length=1, max_length=64)
    value: float | None = None


def _dts_session(sid: str, c) -> "dtsm.Session":
    try:
        return dtsm.STORE.get(sid, c["sub"], admin=c["role"] == "admin")
    except KeyError:
        raise HTTPException(404, "no such training session")


@app.get("/v1/dts/scenarios")
def dts_scenarios(model: str, c=Depends(auth.require("viewer"))):
    net, ver, lib = _dts_net(model)
    return dict(model=model, version=ver, scenarios=lib + [x for k, x in _DTS_CUSTOM.items() if x["model"] == model], simulation_only=True)


@app.post("/v1/dts/scenarios")
def dts_save_scenario(req: DtsScenario, c=Depends(auth.require("engineer"))):
    net, ver, lib = _dts_net(req.model)
    if req.id in {x["id"] for x in lib}:
        raise HTTPException(409, "id belongs to a built-in scenario")
    try:
        sc = dtsm.validate_scenario(net, req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if len(_DTS_CUSTOM) >= 100 and req.id not in _DTS_CUSTOM:
        raise HTTPException(409, "scenario store is full")
    _DTS_CUSTOM[req.id] = dict(sc, model=req.model, author=c["sub"], id=req.id)
    return dict(ok=True, scenario=_DTS_CUSTOM[req.id])


@app.post("/v1/dts/sessions")
def dts_start(req: DtsStart, c=Depends(auth.require("operator"))):
    net, ver, lib = _dts_net(req.model)
    sc = next((x for x in lib if x["id"] == req.scenario), None) or (_DTS_CUSTOM.get(req.scenario) if _DTS_CUSTOM.get(req.scenario, {}).get("model") == req.model else None)
    if sc is None:
        raise HTTPException(404, "unknown scenario")
    try:
        s = dtsm.Session(net, dtsm.validate_scenario(net, sc), c["sub"], req.model)
        dtsm.STORE.add(s)
    except OverflowError as e:
        raise HTTPException(429, str(e))
    return dict(id=s.id, scenario=s.sc, state=s.state, simulation_only=True)


@app.get("/v1/dts/sessions/{sid}")
def dts_get(sid: str, c=Depends(auth.require("operator"))):
    s = _dts_session(sid, c)
    return dict(id=s.id, t=s.t, ended=s.ended, scenario=s.sc, state=s.state, log=s.log[-200:], simulation_only=True)


@app.post("/v1/dts/sessions/{sid}/step")
def dts_step(sid: str, req: DtsStep, c=Depends(auth.require("operator"))):
    s = _dts_session(sid, c)
    try:
        st = s.step(req.dt)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return dict(id=s.id, t=s.t, state=st, done=s.t >= s.sc["duration"] - 1e-9, log=s.log[-200:])


@app.post("/v1/dts/sessions/{sid}/action")
def dts_act(sid: str, req: DtsAct, c=Depends(auth.require("operator"))):
    s = _dts_session(sid, c)
    if req.action in ("set_gen", "shed_load") and req.value is None:
        raise HTTPException(400, "value required")
    try:
        r = s.act(req.action, req.target, req.value)
    except ValueError as e:
        raise HTTPException(400 if "ended" not in str(e) else 409, str(e))
    return dict(**r, t=s.t, log=s.log[-200:])


@app.post("/v1/dts/sessions/{sid}/end")
def dts_end(sid: str, c=Depends(auth.require("operator"))):
    s = _dts_session(sid, c)
    s.ended = True
    return dict(id=s.id, score=s.score(), log=s.log, simulation_only=True)


# ----------------------------------------------------------------------------- distributed contingency analysis
class ShardReq(BaseModel):
    model: str
    version: int
    open_switches: list[str] = Field(default_factory=list, max_length=5000)
    close_switches: list[str] = Field(default_factory=list, max_length=5000)
    jobs: list[list[int]] = Field(max_length=100000)
    vmin: float = Field(default=0.95, ge=0.5, le=1.2)
    vmax: float = Field(default=1.05, ge=0.8, le=1.5)
    enforce_q_limits: bool = False
    workers: int = Field(default=1, ge=1, le=64)


@app.post("/v1/contingency/shard")
def contingency_shard(req: ShardReq, _=Depends(auth.require("viewer"))):
    """Worker side: evaluate a list of AC cases (branch-index lists) at an exact model version."""
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    for j in req.jobs:
        if not j or len(j) > 4 or any(i < 0 or i >= net.nl for i in j):
            raise HTTPException(422, "job refers to a branch outside the network")
    return dist.eval_shard(net, req.jobs, req.vmin, req.vmax, req.enforce_q_limits, req.workers)


class DistReq(CAReq):
    kind: str = Field(default="n1", pattern="^(n1|n2)$")
    k_top: int = Field(default=40, ge=2, le=200)
    verify: int = Field(default=60, ge=1, le=2000)
    top: int = Field(default=25, ge=1, le=500)
    use_peers: bool = True


def _transport_factory(authorization: str, model: str, version: int, req):
    def transport(peer: str, payload: dict) -> dict:
        r = httpx.post(peer + "/v1/contingency/shard", json=payload, headers={"Authorization": authorization}, timeout=float(os.environ.get("EMS_PEER_TIMEOUT", "120")))
        if r.status_code != 200:
            raise RuntimeError(f"peer answered {r.status_code}")
        return r.json()
    return transport


@app.post("/v1/contingency/distributed")
def contingency_distributed(req: DistReq, authorization: str = Header(default=""), _=Depends(auth.require("viewer"))):
    """Coordinator: N-1 (all branches) or N-2 (DC screen here, AC verification of the picked pairs) spread over EMS_PEERS."""
    net, ver = load_net(req.model, req.version, req.open_switches, req.close_switches)
    if req.kind == "n2" and len(net.island_ref) > 1:
        raise HTTPException(422, "distributed N-2 needs a single-island network (the screen would renumber branches)")
    peers = dist.peers_from_env() if req.use_peers else []
    digest = dist.net_digest(net)
    payload = dict(model=req.model, version=ver, open_switches=req.open_switches, close_switches=req.close_switches,
                   vmin=req.vmin, vmax=req.vmax, enforce_q_limits=req.enforce_q_limits, workers=req.workers)
    d = dist.Distributor(peers, _transport_factory(authorization, req.model, ver, req),
                         lambda jobs: dist.eval_shard(net, jobs, req.vmin, req.vmax, req.enforce_q_limits, req.workers), digest)
    info: dict = {}

    def runner(jobs):
        res, i = d.run([list(j) if not isinstance(j, (int, np.integer)) else [int(j)] for j in jobs], payload)
        info.update(i)
        return res
    try:
        if req.kind == "n1":
            r = ca.run_n1(net, workers=1, vmin=req.vmin, vmax=req.vmax, enforce_q_limits=req.enforce_q_limits, runner=runner)
            r["results"] = r["results"][: req.top]
        else:
            r = n2.run_n2(net, workers=1, vmin=req.vmin, vmax=req.vmax, k_top=req.k_top, verify=req.verify,
                          enforce_q_limits=req.enforce_q_limits, runner=runner)
            r["results"] = [{k: v for k, v in x.items() if k != "rank_key"} for x in r["results"][: req.top]]
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    r.update(model=req.model, version=ver, distributed=info or dict(peers=[], shards=0), peers_configured=len(peers))
    return r
