"""Item 18: distributed N-1 / N-2 with fault tolerance. Peers are simulated in-process through the transport hook;
the real HTTP path is exercised by tools/e2e_distributed.py."""
import numpy as np
import pytest
from ems.network import Net
from ems import contingency as ca, n2, distributed as dist, powerflow as pf


@pytest.fixture(scope="module")
def net(bb118):
    return Net.from_busbranch(bb118)


def make(net, peers, behave):
    digest = dist.net_digest(net)
    calls = []

    def transport(peer, payload):
        calls.append(peer)
        mode = behave.get(peer, "ok")
        if mode == "down":
            raise ConnectionError("peer down")
        r = dist.eval_shard(net, payload["jobs"], payload["vmin"], payload["vmax"], payload["enforce_q_limits"])
        if mode == "wrong-model":
            r["digest"] = "0" * 32
        if mode == "short":
            r["results"] = r["results"][:-1]
        return r
    local = lambda jobs: dist.eval_shard(net, jobs, 0.95, 1.05, False)
    d = dist.Distributor(peers, transport, local, digest)
    payload = dict(vmin=0.95, vmax=1.05, enforce_q_limits=False)
    return d, payload, calls


def key(res):
    return [(r["outage"], round(r["severity"], 6), r["converged"]) for r in res["results"]]


def run(net, d, payload):
    info = {}
    def runner(jobs):
        out, i = d.run([list(j) for j in jobs], payload)
        info.update(i)
        return out
    return ca.run_n1(net, runner=runner), info


def test_distributed_n1_equals_single_node(net):
    ref = ca.run_n1(net)
    d, p, _ = make(net, ["http://p1", "http://p2"], {})
    got, info = run(net, d, p)
    assert key(got) == key(ref) and got["cases"] == net.nl
    assert info["shards"] == 3 and all(x["ok"] for x in info["peers"]) and sum(x["cases"] for x in info["peers"]) == net.nl


@pytest.mark.parametrize("mode", ["down", "wrong-model", "short"])
def test_a_bad_peer_changes_nothing_but_the_wall_time(net, mode):
    ref = ca.run_n1(net)
    d, p, calls = make(net, ["http://p1", "http://p2"], {"http://p1": mode})
    got, info = run(net, d, p)
    assert key(got) == key(ref)
    bad = next(x for x in info["peers"] if x["peer"] == "http://p1")
    assert bad["ok"] is False and bad["reassigned_to"] == "http://p2"


def test_all_peers_down_falls_back_to_local(net):
    ref = ca.run_n1(net)
    d, p, _ = make(net, ["http://p1", "http://p2"], {"http://p1": "down", "http://p2": "down"})
    got, info = run(net, d, p)
    assert key(got) == key(ref)
    assert {x["reassigned_to"] for x in info["peers"] if not x["ok"]} == {"local"}


def test_distributed_n2_equals_single_node(net):
    ref = n2.run_n2(net, k_top=15, verify=30)
    d, p, _ = make(net, ["http://p1", "http://p2", "http://p3"], {"http://p2": "down"})
    info = {}
    def runner(jobs):
        out, i = d.run([list(j) for j in jobs], p); info.update(i); return out
    got = n2.run_n2(net, k_top=15, verify=30, runner=runner)
    fx = lambda r: [(x["outage"], x["ac_verified"], round(x.get("severity", -1), 6)) for x in r["results"]]
    assert fx(got) == fx(ref) and got["n_ac_verified"] == ref["n_ac_verified"] > 0


def test_digest_changes_with_the_network(net):
    other = net.without_branches([3])
    assert dist.net_digest(net) != dist.net_digest(other)
    assert dist.net_digest(net) == dist.net_digest(Net.from_busbranch(__import__("json").load(open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "..", "..", "data", "ieee118.busbranch.json")))))


def test_peers_come_only_from_the_environment(monkeypatch):
    monkeypatch.setenv("EMS_PEERS", "http://a:1/, https://b:2,ftp://evil, ,file:///etc")
    assert dist.peers_from_env() == ["http://a:1", "https://b:2"]
