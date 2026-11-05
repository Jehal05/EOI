"""Write and query throughput of the SQLite and PostgreSQL historian stores (synthetic points; one host).
Usage: bench_hist.py postgresql://postgres@127.0.0.1:5433/histbench"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "historian"))
from hist.store import Store

dsn = sys.argv[1]
from hist.pgstore import sql
sql(dsn, "DROP TABLE IF EXISTS raw, agg1m, tags CASCADE")
from hist.pgstore import PgStore
T0 = 1_700_000_000_000
TAGS, SAMPLES = 1707, 120          # one IEEE-118 point list, 120 scans
def batches():
    for i in range(SAMPLES):
        yield [{"t": f"P{j}", "m": T0 + i * 2000, "v": float((i + j) % 97)} for j in range(TAGS)]
for name, s in (("sqlite", Store(os.path.join(tempfile.mkdtemp(), "b.db"))), ("postgresql", PgStore(dsn))):
    t = time.perf_counter(); n = 0
    for b in batches(): n += s.write(b)
    dt = time.perf_counter() - t
    t = time.perf_counter(); r = [s.trend(f"P{k}", T0, T0 + SAMPLES * 2000, 300) for k in range(0, 1707, 57)]; q = (time.perf_counter() - t) / len(r)
    st = s.stats()
    print(f"{name}: {n} rows in {dt:.1f} s = {n / dt:,.0f} rows/s; trend query {q * 1000:.1f} ms avg ({r[0]['resolution']}); {st['bytes_per_raw_row']} bytes/raw row incl. aggregate")
