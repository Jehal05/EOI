"""Tiny client helpers shared by the e2e/benchmark scripts (stdlib + httpx)."""
import base64, hashlib, hmac, struct, time, httpx

CREDS = "/tmp/gs/coreA/dev-credentials.txt"
A, B = "http://127.0.0.1:8083", "http://127.0.0.1:8183"


def totp(b32: str, t=None) -> str:
    key = base64.b32decode(b32 + "=" * (-len(b32) % 8))
    h = hmac.new(key, struct.pack(">Q", int((t or time.time()) // 30)), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 10**6)


def creds():
    out = {}
    for ln in open(CREDS):
        if ln.startswith("#") or not ln.strip():
            continue
        u, pw, role, jur, sec = ln.split()
        out[u] = dict(pw=pw, role=role, jur=jur, totp=sec)
    return out


class Sess:
    def __init__(self, user, base=A):
        self.c = creds()[user]; self.user = user; self.base = base
        self.h = httpx.Client(base_url=base, timeout=30)
        r = self.h.post("/api/login", json={"user": user, "password": self.c["pw"]})
        r.raise_for_status()
        self.tok = r.json()["token"]
        self.h.headers["Authorization"] = "Bearer " + self.tok
        self._last_step = 0

    def code(self):
        # TOTP codes are single-use per 30 s step; wait for a fresh step if needed
        while int(time.time() // 30) <= self._last_step:
            time.sleep(0.5)
        self._last_step = int(time.time() // 30)
        return totp(self.c["totp"])

    def get(self, p, **kw): return self.h.get(p, **kw)
    def post(self, p, **kw): return self.h.post(p, **kw)

    def command(self, tag, action, reason="e2e test"):
        return self.post("/api/command", json={"tag": tag, "action": action, "reason": reason, "totp": self.code()})
