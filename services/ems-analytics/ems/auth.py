"""Verifies/mints the platform's HMAC-SHA256 bearer tokens (same format as cim-pds)."""
import base64, hashlib, hmac, json, os, threading, time
from fastapi import Header, HTTPException

from . import asym_token as _asym
_asym.load_from_env()
_DEV_SECRET = b"dev-secret-do-not-use-in-prod"
ROLE_RANK = {"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}


def secret() -> bytes:
    if _asym.asymmetric():
        return b""   # unused: tokens are Ed25519
    s = os.environ.get("EMS_SECRET", "")
    if len(s) >= 16:
        return s.encode()
    if os.environ.get("EMS_DEV") == "1":
        return _DEV_SECRET
    raise RuntimeError("EMS_SECRET (>=16 chars) required; set EMS_DEV=1 only for local development")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint(sub: str, role: str, jur=("*",), ttl=3600) -> str:
    if _asym.asymmetric():
        return _asym.mint(sub, role, jur, ttl)
    payload = _b64(json.dumps({"sub": sub, "role": role, "jur": list(jur), "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    sig = hmac.new(secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify(token: str) -> dict:
    if _asym.asymmetric():
        try:
            return _asym.verify(token)
        except _asym.TokenError:
            raise HTTPException(401, "invalid token")
    try:
        payload, sig = token.split(".", 1)
        expect = hmac.new(secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            raise ValueError("bad signature")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        raise HTTPException(401, "invalid token")
    if claims["exp"] < time.time():
        raise HTTPException(401, "token expired")
    return claims


class Limiter:
    def __init__(self, window=10.0, maxn=120):
        self.w, self.n, self.s, self.l = window, maxn, {}, threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.l:
            t0, c = self.s.get(key, (now, 0))
            if now - t0 >= self.w:
                t0, c = now, 0
            c += 1
            self.s[key] = (t0, c)
            return c <= self.n


LIMIT = Limiter()


def require(min_role: str):
    def dep(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        c = verify(authorization[7:])
        if not LIMIT.allow(c["sub"]):
            raise HTTPException(429, "rate limit exceeded")
        if ROLE_RANK.get(c["role"], 0) < ROLE_RANK[min_role]:
            raise HTTPException(403, f"role '{c['role']}' cannot perform this action (needs {min_role})")
        return c
    return dep
