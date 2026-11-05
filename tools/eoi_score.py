#!/usr/bin/env python3
"""Maps traceability statuses to a weighted score using docs/eoi_weights.csv.
The EOI scoring rubric was not available to us: the weights file is a TEMPLATE (placeholder rows, blank weights) to be filled
with the real criteria. Rows with a blank weight are reported but not scored. Credit for 'partial' defaults to 0 (conservative)."""
import csv, json, os, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
def score(weights_rows, status):
    tot_w = got = 0.0; lines = []
    for r in weights_rows:
        ids = [x for x in r["requirement_ids"].split(";") if x]
        st = [status.get(i, "not built") for i in ids]
        w = r["weight"].strip()
        if not w: lines.append((r["criterion"], None, st)); continue
        w = float(w)
        cr = {"met": float(r["credit_met"] or 1), "partial": float(r["credit_partial"] or 0), "not built": float(r["credit_not_built"] or 0)}
        c = min((cr.get(s, 0.0) for s in st), default=0.0)   # a criterion earns the credit of its weakest requirement
        tot_w += w; got += w * c; lines.append((r["criterion"], w * c, st))
    return got, tot_w, lines
def main():
    status = {r["id"]: r["status"] for r in json.load(open(f"{ROOT}/docs/traceability.json"))["requirements"]}
    rows = list(csv.DictReader(l for l in open(f"{ROOT}/docs/eoi_weights.csv") if not l.startswith("#")))
    got, tot, lines = score(rows, status)
    for c, s, st in lines: print(f"{c:12} {'not scored (no weight)' if s is None else f'{s:.2f}'}  {st}")
    print(f"scored weight {tot:g}: earned {got:g}" + ("" if tot else "  (no weights filled in: nothing scored)"))
if __name__ == "__main__": main()
