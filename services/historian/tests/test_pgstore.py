"""PostgreSQL store: parity with the SQLite store, idempotent redelivery, retention. Needs HIST_TEST_DSN (a scratch database)."""
import os, tempfile, uuid
import pytest
from hist.store import Store

DSN = os.environ.get("HIST_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="HIST_TEST_DSN not set")


def fresh():
    from hist.pgstore import PgStore, sql
    sql(DSN, "DROP TABLE IF EXISTS raw, agg1m, tags CASCADE")
    return PgStore(DSN)


T0 = 1_700_000_000_000


def pts():
    return [{"t": f"X{j}.P", "m": T0 + i * 1000, "v": float((i * 7 + j) % 50)} for i in range(300) for j in range(3)] + \
           [{"t": "BRK.STATE", "m": T0 + 5000, "s": 2}, {"t": "BRK.STATE", "m": T0 + 9000, "s": 1}]


def test_parity_with_sqlite_store():
    pg, lite = fresh(), Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    assert pg.write(pts()) == lite.write(pts())
    for tag, a, b in (("X1.P", T0, T0 + 600_000), ("X2.P", T0 - 9_000_000, T0 + 9_000_000), ("BRK.STATE", T0, T0 + 20_000)):
        assert pg.trend(tag, a, b, 50) == lite.trend(tag, a, b, 50)
    assert pg.series("X0.P", T0 - 9_000_000, T0 + 9_000_000) == lite.series("X0.P", T0 - 9_000_000, T0 + 9_000_000)
    assert pg.trend("NOPE", 0, 5)["points"] == []


def test_redelivery_is_idempotent():
    pg = fresh()
    batch = pts()
    assert pg.write(batch) == len(batch)
    assert pg.write(batch) == 0                                   # same batch again (at-least-once bus)
    r = pg.trend("X0.P", T0 - 9_000_000, T0 + 9_000_000)
    assert sum(p["n"] for p in r["points"]) == 300               # aggregate not double counted
    assert pg.stats()["raw_rows"] == len(batch)


def test_retention_keeps_the_aggregate():
    pg = fresh()
    pg.write(pts())
    n = pg.purge_raw(T0 + 100_000)
    assert n > 0 and pg.trend("X0.P", T0, T0 + 1000)["points"] == []
    assert sum(p["n"] for p in pg.trend("X0.P", T0 - 9_000_000, T0 + 9_000_000)["points"]) == 300


def test_stats_report_backend():
    s = fresh().stats()
    assert s["backend"] in ("postgresql", "timescaledb") and s["raw_rows"] == 0
