#!/usr/bin/env python3
"""Attach the PUBLIC IEEE-case generator cost polynomials (pypower gencost, quadratic) to the CIM JSON models.
Generators are matched by order (G001... = gen rows). Idempotent. Costs are test data, not real tariffs."""
import json, sys
from pypower.case14 import case14
from pypower.case118 import case118
root = sys.argv[1] if len(sys.argv) > 1 else "data"
for mid, fn in (("ieee14", case14), ("ieee118", case118)):
    gc = fn()["gencost"]
    p = f"{root}/{mid}.cim.json"
    m = json.load(open(p))
    for k, g in enumerate(m["generators"]):
        row = gc[k]
        assert int(row[0]) == 2 and int(row[3]) == 3, "expected quadratic polynomial costs"
        g["costC2"], g["costC1"], g["costC0"] = float(row[4]), float(row[5]), float(row[6])
    m["source"] = m.get("source", "") + ("; " if m.get("source") else "") + "generator costs from public IEEE case gencost"
    json.dump(m, open(p, "w"), indent=1)
    print(mid, "costs attached to", len(m["generators"]), "generators")
