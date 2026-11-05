#!/usr/bin/env python3
"""Usability-study harness. It does NOT contain results: it defines the tasks, records sessions, scores SUS (EN/HI) and reports
task success / time with bootstrap confidence intervals. A real study needs real operators; --selftest only validates the arithmetic.

  usability.py tasks                    list the task script (docs/usability_tasks.json)
  usability.py sus-form [--lang hi]     print the 10 SUS statements
  usability.py add SESSION.json         validate and append a session to docs/usability_sessions.jsonl
  usability.py report                   summary of all recorded sessions
  usability.py verify-tasks             run each task's API success check on the live stack (needs a token in GS_TOKEN)
  usability.py --selftest
Session JSON: {"participant":"P1","lang":"en","role":"operator","years_experience":4,
  "tasks":{"T1":{"success":true,"seconds":42,"errors":0}, ...}, "sus":[4,2,4,1,5,2,4,2,5,1]}  (SUS items 1..10, each 1..5)"""
import json, os, random, statistics, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
TASKS = f"{ROOT}/docs/usability_tasks.json"; SESS = f"{ROOT}/docs/usability_sessions.jsonl"
SUS_EN = ["I think that I would like to use this system frequently.", "I found the system unnecessarily complex.", "I thought the system was easy to use.",
 "I think that I would need the support of a technical person to be able to use this system.", "I found the various functions in this system were well integrated.",
 "I thought there was too much inconsistency in this system.", "I would imagine that most people would learn to use this system very quickly.",
 "I found the system very cumbersome to use.", "I felt very confident using the system.", "I needed to learn a lot of things before I could get going with this system."]
SUS_HI = ["मुझे लगता है कि मैं इस प्रणाली का बार-बार उपयोग करना चाहूँगा।", "मुझे प्रणाली अनावश्यक रूप से जटिल लगी।", "मुझे प्रणाली का उपयोग करना आसान लगा।",
 "मुझे लगता है कि इस प्रणाली को चलाने के लिए मुझे किसी तकनीकी व्यक्ति की सहायता की आवश्यकता होगी।", "मुझे प्रणाली के विभिन्न कार्य भली-भाँति जुड़े हुए लगे।",
 "मुझे लगा कि इस प्रणाली में बहुत अधिक असंगति है।", "मेरा अनुमान है कि अधिकांश लोग इस प्रणाली का उपयोग बहुत जल्दी सीख लेंगे।", "मुझे प्रणाली का उपयोग बहुत बोझिल लगा।",
 "प्रणाली का उपयोग करते समय मुझे बहुत आत्मविश्वास महसूस हुआ।", "इस प्रणाली को शुरू करने से पहले मुझे बहुत कुछ सीखना पड़ा।"]

def sus_score(r):
    """Brooke (1996): odd items contribute r-1, even items 5-r, sum x 2.5 -> 0..100."""
    if len(r) != 10 or any((not isinstance(x, int)) or x < 1 or x > 5 for x in r): raise ValueError("SUS needs 10 integer answers 1..5")
    return 2.5 * sum((x - 1) if i % 2 == 0 else (5 - x) for i, x in enumerate(r))

def bootstrap_ci(xs, stat=statistics.fmean, n=4000, alpha=0.05, seed=1):
    if len(xs) < 2: return None
    rng = random.Random(seed); b = sorted(stat([rng.choice(xs) for _ in xs]) for _ in range(n))
    return b[int(n * alpha / 2)], b[int(n * (1 - alpha / 2)) - 1]

def wilson(k, n, z=1.96):
    if n == 0: return None
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5)
    return (c - h) / d, (c + h) / d

def validate(s, tasks):
    for k in ("participant", "lang", "role", "tasks", "sus"):
        if k not in s: raise ValueError(f"missing {k}")
    if s["lang"] not in ("en", "hi"): raise ValueError("lang must be en or hi")
    ids = {t["id"] for t in tasks}
    for t, v in s["tasks"].items():
        if t not in ids: raise ValueError(f"unknown task {t}")
        if not isinstance(v.get("success"), bool) or not isinstance(v.get("seconds"), (int, float)) or v["seconds"] < 0: raise ValueError(f"bad record for {t}")
    sus_score(s["sus"])

def report(sessions, tasks):
    out = {"participants": len(sessions)}
    sc = [sus_score(s["sus"]) for s in sessions]
    out["sus"] = {"mean": round(statistics.fmean(sc), 1) if sc else None, "ci95": bootstrap_ci(sc), "n": len(sc),
                  "note": "SUS ~68 is the commonly quoted average; with few participants the interval is wide"}
    out["by_lang"] = {l: round(statistics.fmean([sus_score(s["sus"]) for s in sessions if s["lang"] == l]), 1) for l in ("en", "hi") if any(s["lang"] == l for s in sessions)}
    out["tasks"] = {}
    for t in tasks:
        rec = [s["tasks"][t["id"]] for s in sessions if t["id"] in s["tasks"]]
        k = sum(r["success"] for r in rec); tm = [r["seconds"] for r in rec if r["success"]]
        out["tasks"][t["id"]] = {"n": len(rec), "success": k, "success_ci95_wilson": wilson(k, len(rec)),
                                 "median_seconds_successful": statistics.median(tm) if tm else None, "target_seconds": t.get("target_seconds"),
                                 "errors": sum(r.get("errors", 0) for r in rec)}
    return out

def selftest():
    assert sus_score([5, 1] * 5) == 100.0 and sus_score([1, 5] * 5) == 0.0 and sus_score([3] * 10) == 50.0
    assert sus_score([4, 2, 4, 1, 5, 2, 4, 2, 5, 1]) == 85.0            # worked by hand: (3+3+3+4+4+3+3+3+4+4)=34 -> 85
    for bad in ([3] * 9, [0] * 10, [6] * 10, [3.5] * 10):
        try: sus_score(bad); assert False
        except ValueError: pass
    lo, hi = wilson(8, 10); assert 0.49 < lo < 0.51 and 0.94 < hi < 0.96, (lo, hi)   # Wilson 8/10 = [0.490, 0.943]
    xs = [60.0, 70.0, 80.0, 90.0]; lo, hi = bootstrap_ci(xs); assert 60 <= lo <= 75 <= hi <= 90 and bootstrap_ci([1.0]) is None
    assert len(SUS_EN) == len(SUS_HI) == 10
    tasks = json.load(open(TASKS))["tasks"]
    s = {"participant": "SYNTH", "lang": "hi", "role": "operator", "tasks": {"T1": {"success": True, "seconds": 30, "errors": 0}}, "sus": [4, 2, 4, 1, 5, 2, 4, 2, 5, 1]}
    validate(s, tasks); r = report([s, {**s, "lang": "en"}], tasks); assert r["sus"]["mean"] == 85.0 and r["tasks"]["T1"]["success"] == 2
    try: validate({**s, "lang": "fr"}, tasks); assert False
    except ValueError: pass
    print("selftest: all passed (arithmetic only; no study data exists)")

def verify_tasks():
    import urllib.request
    base = os.environ.get("GS_URL", "http://127.0.0.1:8084"); tok = os.environ.get("GS_TOKEN", "")
    bad = 0
    for t in json.load(open(TASKS))["tasks"]:
        c = t["api_check"]
        try:
            rq = urllib.request.Request(base + c["path"], headers={"Authorization": "Bearer " + tok})
            body = urllib.request.urlopen(rq, timeout=10).read().decode(); ok = c["expect"] in body
        except Exception as e: ok = False; body = str(e)
        print(("PASS" if ok else "FAIL"), t["id"], t["title"]); bad += not ok
    sys.exit(1 if bad else 0)

def main(a):
    if "--selftest" in a: return selftest()
    cmd = a[0] if a else "tasks"; tasks = json.load(open(TASKS))["tasks"]
    if cmd == "tasks":
        for t in tasks: print(f"{t['id']}  {t['title']}  (target {t['target_seconds']} s)\n      {t['prompt']}\n      success when: {t['success']}")
    elif cmd == "sus-form":
        for i, s in enumerate(SUS_HI if "hi" in a else SUS_EN, 1): print(f"{i}. {s}  [1 strongly disagree .. 5 strongly agree]")
    elif cmd == "add":
        s = json.load(open(a[1])); validate(s, tasks); open(SESS, "a").write(json.dumps(s, ensure_ascii=False) + "\n"); print("recorded", s["participant"])
    elif cmd == "report":
        ss = [json.loads(l) for l in open(SESS)] if os.path.exists(SESS) else []
        print(json.dumps(report(ss, tasks), indent=1, ensure_ascii=False) if ss else "no sessions recorded: no study has been run")
    elif cmd == "verify-tasks": verify_tasks()
    else: sys.exit(__doc__)
if __name__ == "__main__": main(sys.argv[1:])
