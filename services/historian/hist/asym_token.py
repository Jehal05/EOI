"""Ed25519 "v2" bearer tokens (see services/fep-gateway/internal/token/asym.go for the format).
A process with GS_TOKEN_TRUST set verifies v2 tokens only; GS_TOKEN_KEY (optional) lets it mint."""
import base64, json, os, time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DOMAIN = b"gridsovereign-token-v2\n"
RANK = {"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}
_trust = None      # kid -> (public key, max rank, subject prefix); None = shared-secret mode
_signer = None     # (kid, private key)


class TokenError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_from_env() -> None:
    global _trust, _signer
    tp, kp = os.environ.get("GS_TOKEN_TRUST"), os.environ.get("GS_TOKEN_KEY")
    if tp:
        t = {}
        for k in json.load(open(tp))["keys"]:
            raw = base64.b64decode(k["pub"])
            if len(raw) != 32 or k["maxRole"] not in RANK or not k["kid"]:
                raise SystemExit("invalid trust list entry")
            t[k["kid"]] = (Ed25519PublicKey.from_public_bytes(raw), RANK[k["maxRole"]], k.get("subPrefix", ""))
        if not t:
            raise SystemExit("trust list is empty")
        _trust = t
    if kp and _trust is not None:
        kf = json.load(open(kp))
        seed = base64.b64decode(kf["seed"])
        if len(seed) != 32:
            raise SystemExit("invalid signing key file")
        _signer = (kf["kid"], Ed25519PrivateKey.from_private_bytes(seed))


def asymmetric() -> bool:
    return _trust is not None


def mint(sub: str, role: str, jur=("*",), ttl=3600) -> str:
    if _signer is None:
        raise RuntimeError("asymmetric mode without a signing key (GS_TOKEN_KEY)")
    kid, key = _signer
    p = _b64(json.dumps({"sub": sub, "role": role, "jur": list(jur), "exp": int(time.time()) + ttl, "kid": kid}, separators=(",", ":")).encode())
    return f"v2.{p}.{_b64(key.sign(DOMAIN + p.encode()))}"


def verify(token: str) -> dict:
    try:
        if len(token) > 4096 or not token.startswith("v2."):
            raise TokenError("malformed")
        p, sig = token[3:].split(".")
        sb = _unb64(sig)
        claims = json.loads(_unb64(p))
        k = _trust.get(claims.get("kid", ""))
        if k is None or len(sb) != 64:
            raise TokenError("unknown key")
        try:
            k[0].verify(sb, DOMAIN + p.encode())
        except InvalidSignature:
            raise TokenError("bad signature")
        if RANK.get(claims["role"], 0) == 0 or RANK[claims["role"]] > k[1]:
            raise TokenError("role cap")
        if not str(claims["sub"]).startswith(k[2]):
            raise TokenError("subject prefix")
        if not isinstance(claims["jur"], list) or claims["exp"] < time.time():
            raise TokenError("expired or malformed")
        return claims
    except Exception as e:  # noqa: BLE001
        raise TokenError(str(e))
