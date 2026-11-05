"""Bus consumer against a real redis-server (skipped when none is on HIST_TEST_REDIS)."""
import json, os, tempfile, threading, time
import pytest
from hist.store import Store

URL = os.environ.get("HIST_TEST_REDIS")
pytestmark = pytest.mark.skipif(not URL, reason="HIST_TEST_REDIS not set")


def mk_state():
    return {"connected_to": None, "reconnects": 0, "batches": 0, "last_error": None}


def test_delivery_ack_redelivery_and_poison():
    import redis
    from hist import busconsumer
    r = redis.Redis.from_url(URL, decode_responses=True); r.delete("t:s")
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db")); state = mk_state()
    msg = lambda i: json.dumps({"seq": i, "pts": [{"t": "A.P", "m": 1_700_000_000_000 + i * 1000, "v": float(i)}]})
    for i in range(5):
        r.xadd("t:s", {"d": msg(i)})
    r.xadd("t:s", {"d": "{not json"})                       # poison
    threading.Thread(target=busconsumer.run, args=(st, state, URL, "t:s", "g1", "c1"), daemon=True).start()
    for _ in range(50):
        if st.stats()["raw_rows"] == 5 and r.xpending("t:s", "g1")["pending"] == 0: break
        time.sleep(0.1)
    assert st.stats()["raw_rows"] == 5 and r.xpending("t:s", "g1")["pending"] == 0
    assert "bus message" in (state["last_error"] or "")
    # a consumer that crashed after reading but before ack: its entries are re-read by the next consumer of the same name
    r.xadd("t:s", {"d": msg(9)})
    r.xgroup_create("t:s", "g2", id="0")
    got = r.xreadgroup("g2", "dead", {"t:s": ">"}, count=100)          # g2 reads everything, never acks
    st2 = Store(os.path.join(tempfile.mkdtemp(), "h2.db")); state2 = mk_state()
    threading.Thread(target=busconsumer.run, args=(st2, state2, URL, "t:s", "g2", "dead"), daemon=True).start()
    for _ in range(50):
        if st2.stats()["raw_rows"] == 6 and r.xpending("t:s", "g2")["pending"] == 0: break
        time.sleep(0.1)
    assert st2.stats()["raw_rows"] == 6 and r.xpending("t:s", "g2")["pending"] == 0
