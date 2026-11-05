#!/usr/bin/env python3
"""Assign SYNTHETIC thermal ratings to the public IEEE cases (their own ratings are a
9900 MVA placeholder). rating = max(40, ceil10(1.3 x base-case max-end MVA)).
Ratings are demo data and are labelled as such in the model's `source` field."""
import json, math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "ems-analytics"))
import numpy as np
from ems.network import Net
from ems import powerflow as pf

ROOT = os.path.join(os.path.dirname(__file__), "..")
TAG = "; SYNTHETIC thermal ratings (1.3 x base-case flow, min 40 MVA)"
for mid in ("ieee14", "ieee118"):
    bbp = f"{ROOT}/data/{mid}.busbranch.json"
    net = Net.from_busbranch(json.load(open(bbp)))
    sol = pf.solve(net)
    rating = {}
    for k, br in enumerate(net.br_id):
        rating[br] = float(max(40.0, math.ceil(1.3 * sol["mva_max"][k] / 10.0) * 10.0))
    mp = f"{ROOT}/data/{mid}.cim.json"
    m = json.load(open(mp))
    for b in m["acLineSegments"] + m["transformers"]:
        b["rating"] = rating[b["mrid"]]
    for vl in m["voltageLevels"]:        # SYNTHETIC operating band: IEEE base-case voltages span ~0.94-1.06 pu
        vl["vMin"], vl["vMax"] = 0.93, 1.07
    if TAG not in m["source"]:
        m["source"] += TAG + "; SYNTHETIC voltage operating band 0.93-1.07 pu"
    json.dump(m, open(mp, "w"), indent=1)
    print(mid, "ratings: min", min(rating.values()), "max", max(rating.values()))
