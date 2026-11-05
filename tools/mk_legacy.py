"""Write public IEEE cases (from pypower's bundled data) as MATPOWER .m and PSS/E RAW v33 text, for importer validation.
The RAW text is produced by THIS script from the MATPOWER matrices, so it tests the importer's reading of the layout,
not interoperability with a real PSS/E export."""
import sys, numpy as np
from pypower import case14, case118
out = sys.argv[1] if len(sys.argv) > 1 else "data/legacy"
def fmt(row): return " ".join(repr(float(x)) if x != int(x) else str(int(x)) for x in row)
def mat(name, rows): return f"mpc.{name} = [\n" + "\n".join("\t" + fmt(r) + ";" for r in rows) + "\n];\n"
for nm, f in (("case14", case14.case14), ("case118", case118.case118)):
    c = f()
    c = {k: (v.astype(float) if hasattr(v, 'astype') else v) for k, v in c.items()}
    c['baseMVA'] = float(c['baseMVA'])
    if (c['bus'][:, 9] == 0).all(): c['bus'][:, 9] = 138.0  # the pypower copy of case14 carries no base kV; 138 is assumed (disclosed in STATUS)
    s = f"function mpc = {nm}\nmpc.version = '2';\nmpc.baseMVA = {c['baseMVA']};\n" + mat("bus", c["bus"]) + mat("gen", c["gen"]) + mat("branch", c["branch"]) + mat("gencost", c["gencost"]) + "\n"
    open(f"{out}/{nm}.m", "w").write(s)
    L = [f"0, {c['baseMVA']}, 33, 0, 1, 60.0 / generated from pypower {nm}", "PUBLIC IEEE TEST CASE", "converted by tools/mk_legacy.py"]
    for b in c["bus"].tolist():
        ide = {3: 3, 2: 2, 1: 1, 4: 4}[int(b[1])]
        L.append(f"{int(b[0])},'B{int(b[0])}',{b[9]!r},{ide},{int(b[6])},{int(b[10])},1,{float(b[7])!r},{b[8]!r}")
    L.append("0 / END OF BUS DATA")
    for b in c["bus"].tolist():
        if b[2] or b[3]: L.append(f"{int(b[0])},'1 ',1,1,1,{b[2]!r},{b[3]!r},0,0,0,0,1")
    L.append("0 / END OF LOAD DATA")
    for b in c["bus"].tolist():
        if b[4] or b[5]: L.append(f"{int(b[0])},'1 ',1,{b[4]!r},{b[5]!r}")
    L.append("0 / END OF FIXED SHUNT DATA")
    for g in c["gen"].tolist():
        L.append(f"{int(g[0])},'1 ',{g[1]!r},{g[2]!r},{g[3]!r},{g[4]!r},{g[5]!r},0,{g[6]!r},0,0,0,0,1,{int(g[7]>0)},100,{g[8]!r},{g[9]!r}")
    L.append("0 / END OF GENERATOR DATA")
    tr = []
    for k, r in enumerate(c["branch"].tolist()):
        if r[8] == 0:
            L.append(f"{int(r[0])},{int(r[1])},'{k+1} ',{r[2]!r},{r[3]!r},{r[4]!r},{r[5]!r},0,0,0,0,0,0,{int(r[10])},1,0")
        else: tr.append((k, r))
    L.append("0 / END OF BRANCH DATA")
    for k, r in tr:
        L += [f"{int(r[0])},{int(r[1])},0,'{k+1} ',1,1,1,0,0,2,'XF{k+1}',{int(r[10])}", f"{r[2]!r},{r[3]!r},{c['baseMVA']}", f"{r[8]!r},0.0,{r[9]!r},{r[5]!r},0,0,0,0,1.1,0.9,1.1,0.9,33,0,0,0", "1.0,0.0"]
    L.append("0 / END OF TRANSFORMER DATA")
    open(f"{out}/{nm}.raw", "w").write("\n".join(L) + "\n")
    print(nm, "ok")
