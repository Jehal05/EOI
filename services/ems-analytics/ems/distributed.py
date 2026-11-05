"""Distributed contingency analysis: a coordinator splits AC cases over peer EMS processes and merges the results.

Design
 * Peers are named ONLY by the operator in the EMS_PEERS environment variable (comma-separated base URLs). A request can
   never name a peer, so the endpoint cannot be used to make the service call arbitrary addresses (SSRF).
 * Each peer rebuilds the network itself from cim-pds at the SAME model version, switch overlay and options, and returns a
   digest of the network arrays. The coordinator accepts a shard only if the digest equals its own, so a peer holding a
   different model can never contribute to the answer.
 * Cases are dealt round-robin to workers (the coordinator is one of them), which balances out the cost differences between
   cases. A shard whose peer fails, times out or returns a wrong digest is re-run: on the next healthy peer if there is
   one, otherwise locally. The merged answer is therefore the same whatever fails; only the wall time changes.
 * The caller's bearer token is forwarded; every peer applies the same authorisation.
"""
from __future__ import annotations
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from .network import Net
from . import contingency as ca, powerflow as pf


def net_digest(net: Net) -> str:
    h = hashlib.sha256()
    for a in (net.base, net.f, net.t, net.r, net.x, net.b, net.tap, net.shift, net.rate, net.pd, net.qd, net.g_bus, net.g_p, net.g_vset):
        h.update(np.ascontiguousarray(np.asarray(a, dtype=float)).tobytes())
    h.update("|".join(net.br_id).encode())
    return h.hexdigest()[:32]


def peers_from_env() -> list[str]:
    return [p.strip().rstrip("/") for p in os.environ.get("EMS_PEERS", "").split(",") if p.strip().startswith(("http://", "https://"))]


def eval_shard(net: Net, jobs: list[list[int]], vmin: float, vmax: float, enforce_q: bool, workers: int = 1) -> dict:
    """Run the AC cases in `jobs` (each a list of branch indices) on this process."""
    t0 = time.perf_counter()
    base = pf.solve(net, enforce_q_limits=enforce_q)
    if not base["converged"]:
        raise RuntimeError("base case did not converge")
    bo, bv = ca.violations(net, base, vmin, vmax)
    args = (net, base["V"], ({b: l for b, l in bo}, {b: v for b, v in bv}), vmin, vmax, enforce_q)
    ks = [tuple(j) if len(j) > 1 else int(j[0]) for j in jobs]
    if workers > 1 and len(ks) > 50:
        with ca.mp_context().Pool(workers, initializer=ca._init, initargs=args) as pool:
            res = pool.map(ca._one, ks, chunksize=max(1, len(ks) // (workers * 4)))
    else:
        ca._init(*args)
        res = [ca._one(k) for k in ks]
    return dict(results=res, digest=net_digest(net), ms=(time.perf_counter() - t0) * 1000.0)


class Distributor:
    """transport(peer_url, payload) -> dict raises on failure. `local(jobs)` -> dict like eval_shard."""

    def __init__(self, peers: list[str], transport, local, digest: str, max_workers: int = 16):
        self.peers, self.transport, self.local, self.digest = list(peers), transport, local, digest
        self.max_workers = max_workers

    def run(self, jobs: list[list[int]], payload: dict) -> tuple[list[dict], dict]:
        t0 = time.perf_counter()
        slots = ["local"] + self.peers
        shards = {s: jobs[i::len(slots)] for i, s in enumerate(slots)}
        stats = {s: dict(peer=s, cases=len(shards[s]), ok=True, ms=None, reassigned_to=None, error=None) for s in slots}
        out: dict[str, list[dict]] = {}

        def work(s):
            if not shards[s]:
                return s, [], None
            try:
                if s == "local":
                    r = self.local(shards[s])
                else:
                    r = self.transport(s, dict(payload, jobs=shards[s]))
                    if r.get("digest") != self.digest:
                        raise RuntimeError("peer holds a different network (digest mismatch)")
                    if len(r["results"]) != len(shards[s]):
                        raise RuntimeError("peer returned the wrong number of results")
                return s, r["results"], r["ms"]
            except Exception as e:  # noqa: BLE001 - any failure of a peer is handled the same way
                return s, None, str(e)[:200]

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(slots))) as ex:
            for s, res, info in ex.map(work, slots):
                if res is None:
                    stats[s].update(ok=False, error=info)
                else:
                    out[s] = res
                    stats[s]["ms"] = info
        failed = [s for s in slots if not stats[s]["ok"]]
        healthy = [s for s in self.peers if stats[s]["ok"]]
        for i, s in enumerate(failed):                       # re-run lost shards: healthy peer first, else locally
            target = None
            for t in ([healthy[i % len(healthy)]] if healthy else []) + ["local"]:
                try:
                    r = self.local(shards[s]) if t == "local" else self.transport(t, dict(payload, jobs=shards[s]))
                    if t != "local" and (r.get("digest") != self.digest or len(r["results"]) != len(shards[s])):
                        raise RuntimeError("bad retry result")
                    out[s] = r["results"]; target = t
                    break
                except Exception:  # noqa: BLE001
                    continue
            if target is None:
                raise RuntimeError(f"shard of {s} could not be evaluated anywhere")
            stats[s]["reassigned_to"] = target
        merged: list[dict | None] = [None] * len(jobs)
        for i, s in enumerate(slots):                         # undo the round-robin deal: result order == job order
            for pos, r in zip(range(i, len(jobs), len(slots)), out.get(s, [])):
                merged[pos] = r
        if any(m is None for m in merged):                  # not an assert: asserts vanish under python -O
            raise RuntimeError("distributed run returned an incomplete result")
        return merged, dict(peers=list(stats.values()), wall_seconds=round(time.perf_counter() - t0, 3), shards=len(slots))
