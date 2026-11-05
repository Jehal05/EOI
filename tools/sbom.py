#!/usr/bin/env python3
"""Software bill of materials with licences for everything that ships or builds the product.
Writes docs/SBOM.cdx.json (CycloneDX 1.5) and docs/SBOM.md and applies a licence policy: permissive licences pass,
weak copyleft (MPL, LGPL) and unknown/missing are flagged for review, strong copyleft (GPL, AGPL, SSPL) and non-commercial fail.
Sources: Cargo (cargo metadata, normal+build graph of the release binary), npm (package-lock.json, production vs dev),
Python (installed distributions reachable from the requirements files, metadata read from the environment), Go (go.mod:
standard library only), Java (JDK only), container base images (declared, versions not pinned by digest)."""
import importlib.metadata as md, json, os, re, subprocess, sys, datetime, hashlib
ROOT = os.path.join(os.path.dirname(__file__), "..")
OK = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Zlib", "Unicode-3.0", "Unicode-DFS-2016", "0BSD", "CC0-1.0", "BlueOak-1.0.0", "Python-2.0", "PSF-2.0", "Unlicense", "BSL-1.0", "CC-BY-4.0", "WTFPL", "Apache-2.0 WITH LLVM-exception", "LLVM-exception", "OpenSSL"}
REVIEW = {"MPL-2.0", "LGPL-2.1", "LGPL-3.0", "EPL-2.0", "CDDL-1.0", "OFL-1.1"}
BAD = re.compile(r"(^|[^L])GPL|AGPL|SSPL|BUSL|Commons-Clause|CC-BY-NC|RSAL|Elastic", re.I)

def norm(l):
    l = (l or "").strip()
    if "Mozilla Public License 2.0" in l: return "MPL-2.0"
    if l == "LGPL-3.0-only": return "LGPL-3.0"
    m = {"MIT License": "MIT", "Apache 2.0": "Apache-2.0", "Apache Software License": "Apache-2.0", "BSD": "BSD-3-Clause", "BSD License": "BSD-3-Clause", "Apache License 2.0": "Apache-2.0",
         "Apache-2.0 OR MIT": "Apache-2.0 OR MIT", "BSD-3-Clause OR Apache-2.0": "BSD-3-Clause OR Apache-2.0"}
    return m.get(l, l)

def classify(expr):
    """An SPDX expression passes if, for every AND-term, at least one OR-alternative is permissive."""
    if not expr: return "unknown"
    worst = "ok"
    for term in re.split(r"\s+AND\s+", expr.replace("(", "").replace(")", "").replace("/", " OR ")):
        alts = [a.strip() for a in re.split(r"\s+OR\s+", term)]
        def one(a):
            a = a.replace(" WITH LLVM-exception", "")
            if a in OK or a.startswith(("Apache-2.0", "BSD", "MIT")): return "ok"
            if BAD.search(a): return "fail"
            if a in REVIEW: return "review"
            return "unknown"
        rank = {"ok": 0, "review": 1, "unknown": 2, "fail": 3}
        best = min((one(a) for a in alts), key=rank.get)
        if rank[best] > rank[worst]: worst = best
    return worst

comps = []
def add(eco, name, ver, lic, scope="required", src=None, h=None):
    comps.append(dict(eco=eco, name=name, version=ver, license=norm(lic), scope=scope, source=src, hash=h))

# ---- Rust
md_ = json.loads(subprocess.check_output(["cargo", "metadata", "--format-version", "1", "--locked", "--filter-platform", "x86_64-unknown-linux-gnu"], cwd=f"{ROOT}/services/cim-pds", stderr=subprocess.DEVNULL))
pk = {p["id"]: p for p in md_["packages"]}; nodes = {n["id"]: n for n in md_["resolve"]["nodes"]}
root = md_["resolve"]["root"]; seen = set(); stack = [root]
while stack:
    i = stack.pop()
    if i in seen: continue
    seen.add(i)
    for d in nodes[i]["deps"]:
        if any(k["kind"] in (None, "build") for k in d["dep_kinds"]): stack.append(d["pkg"])
for i in seen:
    p = pk[i]
    if i == root: continue
    add("cargo", p["name"], p["version"], p.get("license") or p.get("license_file") and "SEE-LICENSE-FILE" or "", "required", p.get("source"))
# ---- npm
lock = json.load(open(f"{ROOT}/ui/package-lock.json"))
for path, v in lock["packages"].items():
    if not path: continue
    add("npm", path.split("node_modules/")[-1], v.get("version", ""), v.get("license") or "", "optional" if v.get("dev") else "required", v.get("resolved"), v.get("integrity"))
# ---- Python (closure over Requires-Dist, from the active environment)
def reqs(f): return [re.split(r"[<>=!~\[; ]", l.strip())[0] for l in open(f) if l.strip() and not l.startswith("#")]
want = []; [want.extend(reqs(f"{ROOT}/services/{s}/requirements.txt")) for s in ("ems-analytics", "historian")]
seen_py = {}
def canon(n): return re.sub(r"[-_.]+", "-", n).lower()
stack = list(dict.fromkeys(want))
while stack:
    n = stack.pop()
    if canon(n) in seen_py: continue
    try: d = md.distribution(n)
    except md.PackageNotFoundError: continue
    seen_py[canon(n)] = d
    for r in d.requires or []:
        if "extra ==" in r: continue
        stack.append(re.split(r"[<>=!~\[; (]", r.strip())[0])
test_only = {"pytest", "iniconfig", "pluggy", "packaging", "pygments"}
for n, d in sorted(seen_py.items()):
    m = d.metadata; lic = m.get("License-Expression") or ""
    if not lic:
        cl = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = " OR ".join(norm(c) for c in cl) or norm((m.get("License") or "").split("\n")[0][:60])
    add("pypi", m["Name"], m["Version"], lic, "optional" if n in test_only else "required")
# ---- declared, not enumerable from lock files
add("go", "Go standard library only (go.mod has no requirements)", "1.24", "BSD-3-Clause")
add("java", "OpenJDK 21 runtime (no third-party jars; the build uses javac only)", "21", "GPL-2.0 WITH Classpath-exception-2.0", "required")
for n, v, l in (("postgres (container image)", "16", "PostgreSQL"), ("redis (container image, pin to 7.2.x: later releases changed licence)", "7.2", "BSD-3-Clause"), ("eclipse-temurin (container base image)", "21", "GPL-2.0 WITH Classpath-exception-2.0"), ("distroless static/cc (container base image)", "debian12", "Apache-2.0"), ("python (container base image)", "3.13-slim", "PSF-2.0"), ("rust (build image)", "1.82", "MIT OR Apache-2.0"), ("golang (build image)", "1.23", "BSD-3-Clause")):
    add("container", n, v, l, "required")
for c in comps:
    e = c["license"]
    c["policy"] = "ok" if "Classpath-exception" in e or e == "PostgreSQL" else classify(e)
bad = [c for c in comps if c["policy"] == "fail"]; rev = [c for c in comps if c["policy"] in ("review", "unknown")]

# ---- outputs
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def purl(c):
    t = {"cargo": "cargo", "npm": "npm", "pypi": "pypi"}.get(c["eco"])
    return f"pkg:{t}/{c['name']}@{c['version']}" if t else None
cdx = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "metadata": {"timestamp": now, "component": {"type": "application", "name": "gridsovereign", "version": "0.1.0"}, "tools": [{"name": "tools/sbom.py"}]},
       "components": [{"type": "library" if c["eco"] != "container" else "container", "name": c["name"], "version": c["version"], "scope": c["scope"], **({"purl": purl(c)} if purl(c) else {}),
                       "licenses": [{"expression": c["license"]}] if c["license"] else [], "properties": [{"name": "gs:ecosystem", "value": c["eco"]}, {"name": "gs:policy", "value": c["policy"]}]} for c in comps]}
json.dump(cdx, open(f"{ROOT}/docs/SBOM.cdx.json", "w"), indent=1)
from collections import Counter
by = Counter((c["eco"], c["scope"]) for c in comps); lic = Counter(c["license"] or "(none)" for c in comps if c["eco"] not in ("container", "go", "java"))
L = [f"# Software bill of materials\n", f"Generated {now} by `tools/sbom.py` (machine-readable: `docs/SBOM.cdx.json`, CycloneDX 1.5). {len(comps)} components.\n",
     "| Ecosystem | Required (ships or builds the product) | Optional (test/dev only) |", "|---|---|---|"]
for e in ("cargo", "npm", "pypi", "go", "java", "container"):
    L.append(f"| {e} | {by[(e, 'required')]} | {by[(e, 'optional')]} |")
L += ["", "## Licence policy result", f"* Fail (strong copyleft / non-commercial): **{len(bad)}**", f"* Needs review (weak copyleft or unknown): **{len(rev)}**", ""]
for c in bad + rev: L.append(f"  * `{c['eco']}:{c['name']} {c['version']}` — {c['license'] or 'no licence field'} → {c['policy']}")
L += ["", "## Licences in use (libraries)", "", "| Licence expression | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in lic.most_common()]
L += ["", "## Limits", "* Python versions are those installed in the build environment, not a hash-locked requirements file; `requirements.txt` uses lower bounds only.",
      "* Container base images are named by tag, not by digest, and were never built here.", "* Licence fields are as declared by the packages; no licence texts were audited and no copyright notices collected.",
      "* The JDK is GPLv2 with the Classpath exception: fine for running our code, to be confirmed by counsel for redistribution inside an image.",
      "* No transitive vulnerability scan beyond `npm audit` and `pip-audit` (run by `tools/sast.sh`); `cargo audit` could not be installed here."]
open(f"{ROOT}/docs/SBOM.md", "w").write("\n".join(L) + "\n")
print(f"{len(comps)} components; policy fail {len(bad)}, review/unknown {len(rev)}")
for c in bad + rev: print("  ", c["eco"], c["name"], c["version"], "|", c["license"], "->", c["policy"])
sys.exit(1 if bad else 0)
