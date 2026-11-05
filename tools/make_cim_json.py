#!/usr/bin/env python3
"""Convert public IEEE test cases into the platform's node-breaker CIM JSON.

Source data: public IEEE cases (via the BSD-licensed `pypower` case files),
used ONLY as test/validation data. Geography is a synthetic schematic layout,
not real Indian grid data.

Node-breaker construction (synthetic): every bus becomes one Substation with one
VoltageLevel and one busbar ConnectivityNode. Every branch end, generator, load
and shunt connects through its own terminal node and a Breaker to the busbar.
"""
import json
import sys
import math
import numpy as np

REGIONS = ["Northern", "Western", "Southern", "Eastern", "North-Eastern"]


def layout(n, edges, seed=7):
    """Deterministic spring layout (Fruchterman-Reingold) in [0,1]^2."""
    rng = np.random.default_rng(seed)
    pos = rng.random((n, 2))
    k = math.sqrt(1.0 / n)
    t = 0.1
    for _ in range(300):
        d = pos[:, None, :] - pos[None, :, :]
        dist = np.linalg.norm(d, axis=2) + 1e-9
        rep = (k * k / dist ** 2)[:, :, None] * d
        disp = rep.sum(axis=1)
        for a, b in edges:
            dv = pos[a] - pos[b]
            dd = np.linalg.norm(dv) + 1e-9
            f = dv * (dd / k)
            disp[a] -= f
            disp[b] += f
        length = np.linalg.norm(disp, axis=1)[:, None] + 1e-9
        pos += disp / length * np.minimum(length, t)
        t *= 0.98
    pos -= pos.min(axis=0)
    pos /= pos.max(axis=0)
    return pos


def convert(case, model_id, name):
    base = float(case["baseMVA"])
    bus = np.array(case["bus"], dtype=float)
    gen = np.array(case["gen"], dtype=float)
    br = np.array(case["branch"], dtype=float)
    n = len(bus)
    idx = {int(b[0]): i for i, b in enumerate(bus)}
    edges = [(idx[int(r[0])], idx[int(r[1])]) for r in br]
    pos = layout(n, edges)
    missing_kv = any(float(b[9]) <= 0 for b in bus)
    # schematic placement inside India's bounding box
    lat = 8.5 + pos[:, 1] * 27.0
    lon = 69.0 + pos[:, 0] * 27.0

    m = {
        "id": model_id,
        "name": name,
        "baseMVA": base,
        "source": "public IEEE test case (validation data); synthetic node-breaker topology and layout"
                  + ("; source has no baseKV, synthetic 132 kV assigned" if missing_kv else ""),
        "substations": [], "voltageLevels": [], "connectivityNodes": [],
        "switches": [], "acLineSegments": [], "transformers": [],
        "generators": [], "loads": [], "shunts": [],
    }
    bus_node = {}
    for i, b in enumerate(bus):
        num = int(b[0])
        ss = f"SS-{num:04d}"
        vl = f"VL-{num:04d}"
        nd = f"CN-{num:04d}-BB"
        m["substations"].append({
            "mrid": ss, "name": f"Substation {num}", "region": REGIONS[(num * 7) % len(REGIONS)],
            "lat": round(float(lat[i]), 4), "lon": round(float(lon[i]), 4)})
        kv = float(b[9]) if float(b[9]) > 0 else 132.0
        m["voltageLevels"].append({"mrid": vl, "name": f"{ss} {kv:.0f}kV", "substation": ss, "nominalKV": kv})
        m["connectivityNodes"].append({"mrid": nd, "name": f"Busbar {num}", "voltageLevel": vl})
        bus_node[num] = (nd, vl)
        # IEEE bus data initial voltage (flat-start reference; solved values come from the EMS)
        m["substations"][-1]["vm0"] = float(b[7])
        m["substations"][-1]["va0"] = float(b[8])

    def term(label, bnum):
        nd, vl = bus_node[bnum]
        tn = f"CN-{label}"
        m["connectivityNodes"].append({"mrid": tn, "name": f"Terminal {label}", "voltageLevel": vl})
        m["switches"].append({"mrid": f"BRK-{label}", "name": f"Breaker {label}", "type": "Breaker",
                              "node1": nd, "node2": tn, "open": False})
        return tn

    for k, r in enumerate(br):
        f, t = int(r[0]), int(r[1])
        label_f, label_t = f"BR{k+1:04d}-F", f"BR{k+1:04d}-T"
        n1, n2 = term(label_f, f), term(label_t, t)
        tap = float(r[8])
        entry = {"mrid": f"BR{k+1:04d}", "name": f"Branch {f}-{t}", "r": float(r[2]), "x": float(r[3]),
                 "bch": float(r[4]), "rating": float(r[5]), "node1": n1, "node2": n2}
        if tap != 0.0 or float(r[9]) != 0.0:
            entry.update({"ratio": tap if tap != 0.0 else 1.0, "angle": float(r[9])})
            m["transformers"].append(entry)
        else:
            m["acLineSegments"].append(entry)
        if int(r[10]) == 0:  # out of service in source data -> open both breakers
            for sw in m["switches"][-2:]:
                sw["open"] = True

    for k, g in enumerate(gen):
        num = int(g[0])
        label = f"G{k+1:03d}"
        tn = term(label, num)
        m["generators"].append({
            "mrid": label, "name": f"Generator {k+1} @bus {num}", "node": tn,
            "p": float(g[1]), "q": float(g[2]), "qmax": float(g[3]), "qmin": float(g[4]),
            "vset": float(g[5]), "pmax": float(g[8]), "pmin": float(g[9]),
            "inService": bool(int(g[7]) > 0),
            "fuel": ["thermal", "hydro", "gas", "solar", "wind"][k % 5]})
    for i, b in enumerate(bus):
        num = int(b[0])
        if b[2] != 0 or b[3] != 0:
            tn = term(f"L{num:04d}", num)
            m["loads"].append({"mrid": f"L{num:04d}", "name": f"Load @bus {num}", "node": tn,
                               "p": float(b[2]), "q": float(b[3])})
        if b[4] != 0 or b[5] != 0:
            tn = term(f"SH{num:04d}", num)
            m["shunts"].append({"mrid": f"SH{num:04d}", "name": f"Shunt @bus {num}", "node": tn,
                                "g": float(b[4]), "b": float(b[5])})
    # reference (slack) bus from IEEE bus type 3
    ref_buses = [int(b[0]) for b in bus if int(b[1]) == 3]
    ref = ref_buses[0]
    m["slackSubstation"] = f"SS-{ref:04d}"
    return m


def main():
    from pypower.case14 import case14
    from pypower.case118 import case118
    out = sys.argv[1] if len(sys.argv) > 1 else "data"
    for cid, fn, nm in (("ieee14", case14, "IEEE 14-bus (synthetic node-breaker)"),
                        ("ieee118", case118, "IEEE 118-bus (synthetic node-breaker)")):
        m = convert(fn(), cid, nm)
        with open(f"{out}/{cid}.cim.json", "w") as fh:
            json.dump(m, fh, indent=1)
        print(cid, {k: len(v) for k, v in m.items() if isinstance(v, list)})


if __name__ == "__main__":
    main()
