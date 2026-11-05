"""Bus-branch network used by all EMS functions (built from cim-pds output).

Conventions follow the usual transmission-planning convention: per-unit
impedances on baseMVA, tap ratio 0 means 'no transformer' (treated as 1.0),
phase shift in degrees, bus types 1=PQ 2=PV 3=REF 4=dead/isolated.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components

UNRATED = 9000.0  # ratings at/above this (or 0) are treated as "no limit defined"


@dataclass
class Net:
    base: float
    bus_name: list
    base_kv: np.ndarray
    pd: np.ndarray
    qd: np.ndarray
    gs: np.ndarray
    bs: np.ndarray
    # branches
    br_id: list
    f: np.ndarray
    t: np.ndarray
    r: np.ndarray
    x: np.ndarray
    b: np.ndarray
    tap: np.ndarray
    shift: np.ndarray
    rate: np.ndarray
    # generators (in service only)
    g_id: list
    g_bus: np.ndarray
    g_p: np.ndarray
    g_q: np.ndarray
    g_qmax: np.ndarray
    g_qmin: np.ndarray
    g_vset: np.ndarray
    g_pmax: np.ndarray
    g_pmin: np.ndarray
    slack_bus: int = 0
    # filled by analyze_islands()
    island: np.ndarray = field(default=None)
    bus_type: np.ndarray = field(default=None)
    island_ref: dict = field(default_factory=dict)  # island id -> ref bus (or None if dead)
    # load mrid -> (bus index, p MW); lets live telemetry override individual loads
    load_bus: dict = field(default_factory=dict)
    load_p0: dict = field(default_factory=dict)
    # optional generator cost polynomials (c2*P^2 + c1*P + c0, P in MW); NaN where the model has none
    g_c2: np.ndarray = field(default=None)
    g_c1: np.ndarray = field(default=None)
    g_c0: np.ndarray = field(default=None)

    @property
    def nb(self):
        return len(self.bus_name)

    @property
    def nl(self):
        return len(self.f)

    # ------------------------------------------------------------------ build
    @staticmethod
    def from_busbranch(d: dict) -> "Net":
        buses, brs, gens = d["buses"], d["branches"], d["gens"]
        a = lambda xs, k, dt=float: np.array([x[k] for x in xs], dtype=dt)
        n = Net(
            base=float(d["baseMVA"]),
            bus_name=[b["name"] for b in buses],
            base_kv=a(buses, "baseKV"), pd=a(buses, "pd"), qd=a(buses, "qd"),
            gs=a(buses, "gs"), bs=a(buses, "bs"),
            br_id=[x["mrid"] for x in brs],
            f=a(brs, "from", int), t=a(brs, "to", int), r=a(brs, "r"), x=a(brs, "x"), b=a(brs, "b"),
            tap=a(brs, "tap"), shift=a(brs, "shift"), rate=a(brs, "rate"),
            g_id=[g["mrid"] for g in gens], g_bus=a(gens, "bus", int), g_p=a(gens, "p"), g_q=a(gens, "q"),
            g_qmax=a(gens, "qmax"), g_qmin=a(gens, "qmin"), g_vset=a(gens, "vset"),
            g_pmax=a(gens, "pmax"), g_pmin=a(gens, "pmin"),
        )
        for k, key in (("g_c2", "costC2"), ("g_c1", "costC1"), ("g_c0", "costC0")):
            setattr(n, k, np.array([g.get(key, np.nan) if g.get(key) is not None else np.nan for g in gens], dtype=float))
        n.load_bus = {l["mrid"]: int(l["bus"]) for l in d.get("loads", [])}
        n.load_p0 = {l["mrid"]: float(l["p"]) for l in d.get("loads", [])}
        slack = [i["refBus"] for i in d["islands"] if i.get("hasSlack") and i.get("refBus") is not None]
        n.slack_bus = slack[0] if slack else 0
        n.analyze_islands()
        return n

    def copy(self) -> "Net":
        import copy
        return copy.deepcopy(self)

    # ------------------------------------------------------------- topology
    def analyze_islands(self):
        """Connected components over in-service branches; choose a reference bus per island.
        Island containing the system slack uses it; otherwise the largest generator's bus;
        islands with no generation are dead (de-energised)."""
        nb = self.nb
        if self.nl:
            A = sp.coo_matrix((np.ones(self.nl), (self.f, self.t)), shape=(nb, nb))
        else:
            A = sp.coo_matrix((nb, nb))
        _, lab = connected_components(A, directed=False)
        self.island = lab
        self.island_ref = {}
        gen_isl = lab[self.g_bus] if len(self.g_bus) else np.array([], dtype=int)
        for k in np.unique(lab):
            if lab[self.slack_bus] == k:
                self.island_ref[int(k)] = int(self.slack_bus)
                continue
            m = np.where(gen_isl == k)[0]
            self.island_ref[int(k)] = int(self.g_bus[m[np.argmax(self.g_pmax[m])]]) if len(m) else None
        bt = np.ones(nb, dtype=int)
        bt[np.unique(self.g_bus)] = 2
        for k, rb in self.island_ref.items():
            if rb is None:
                bt[lab == k] = 4
            else:
                bt[rb] = 3
        self.bus_type = bt

    def without_branches(self, idx) -> "Net":
        """New network with the given branch indices removed (outage)."""
        keep = np.ones(self.nl, dtype=bool)
        keep[list(idx)] = False
        n = self.copy()
        for k in ("f", "t", "r", "x", "b", "tap", "shift", "rate"):
            setattr(n, k, getattr(self, k)[keep])
        n.br_id = [b for b, kp in zip(self.br_id, keep) if kp]
        n.analyze_islands()
        return n

    def subnet(self, k: int):
        """Extract island k as an independent Net. Returns (net, bus_map) where bus_map[i] = original bus."""
        buses = np.where(self.island == k)[0]
        newi = -np.ones(self.nb, dtype=int)
        newi[buses] = np.arange(len(buses))
        bm = np.where(self.island[self.f] == k)[0]
        gm = np.where(self.island[self.g_bus] == k)[0] if len(self.g_bus) else np.array([], dtype=int)
        s = Net(
            base=self.base, bus_name=[self.bus_name[i] for i in buses], base_kv=self.base_kv[buses],
            pd=self.pd[buses], qd=self.qd[buses], gs=self.gs[buses], bs=self.bs[buses],
            br_id=[self.br_id[i] for i in bm], f=newi[self.f[bm]], t=newi[self.t[bm]],
            r=self.r[bm], x=self.x[bm], b=self.b[bm], tap=self.tap[bm], shift=self.shift[bm], rate=self.rate[bm],
            g_id=[self.g_id[i] for i in gm], g_bus=newi[self.g_bus[gm]], g_p=self.g_p[gm], g_q=self.g_q[gm],
            g_qmax=self.g_qmax[gm], g_qmin=self.g_qmin[gm], g_vset=self.g_vset[gm],
            g_pmax=self.g_pmax[gm], g_pmin=self.g_pmin[gm],
            g_c2=None if self.g_c2 is None else self.g_c2[gm], g_c1=None if self.g_c1 is None else self.g_c1[gm],
            g_c0=None if self.g_c0 is None else self.g_c0[gm],
            slack_bus=int(newi[self.island_ref[k]]) if self.island_ref.get(k) is not None else 0,
            load_bus={m: int(newi[b]) for m, b in self.load_bus.items() if newi[b] >= 0}, load_p0={m: v for m, v in self.load_p0.items() if self.load_bus.get(m) is not None and newi[self.load_bus[m]] >= 0},
        )
        s.analyze_islands()
        return s, buses

    # ---------------------------------------------------------------- admittance
    def ybus(self):
        """Return (Ybus, Yf, Yt) sparse matrices."""
        nb, nl = self.nb, self.nl
        ys = 1.0 / (self.r + 1j * self.x)
        bc = self.b
        tap = np.where(self.tap != 0.0, self.tap, 1.0).astype(complex) * np.exp(1j * np.pi / 180.0 * self.shift)
        ytt = ys + 1j * bc / 2.0
        yff = ytt / (tap * np.conj(tap))
        yft = -ys / np.conj(tap)
        ytf = -ys / tap
        ysh = (self.gs + 1j * self.bs) / self.base
        i = np.arange(nl)
        Yf = sp.csr_matrix((np.r_[yff, yft], (np.r_[i, i], np.r_[self.f, self.t])), shape=(nl, nb))
        Yt = sp.csr_matrix((np.r_[ytf, ytt], (np.r_[i, i], np.r_[self.f, self.t])), shape=(nl, nb))
        Cf = sp.csr_matrix((np.ones(nl), (i, self.f)), shape=(nl, nb))
        Ct = sp.csr_matrix((np.ones(nl), (i, self.t)), shape=(nl, nb))
        Ybus = (Cf.T @ Yf + Ct.T @ Yt + sp.diags(ysh)).tocsr()
        return Ybus, Yf.tocsr(), Yt.tocsr()

    def conn(self):
        i = np.arange(self.nl)
        Cf = sp.csr_matrix((np.ones(self.nl), (i, self.f)), shape=(self.nl, self.nb))
        Ct = sp.csr_matrix((np.ones(self.nl), (i, self.t)), shape=(self.nl, self.nb))
        return Cf, Ct
