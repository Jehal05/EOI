"""PostgreSQL time-series store with the same interface as hist.store.Store.

- Writes are idempotent: a raw row (tag, ts) that already exists is left alone and does NOT touch the 1-minute aggregate,
  so an at-least-once bus can redeliver without double counting (first write wins; SQLite store overwrites instead).
- If the TimescaleDB extension is installed the raw table becomes a hypertable (7-day chunks); without it the same schema is
  plain PostgreSQL, partition-free. The Timescale path is written to the documented API but has NOT been run here.
- Retention: purge_raw(before_ms) drops raw rows; the 1-minute aggregate is kept.
Driver: pg8000 (BSD-3, pure Python; chosen over psycopg because psycopg is LGPL). No connection pool (keeps dependencies small): one writer connection under a lock, a short-lived connection per read.
"""
from __future__ import annotations
import threading, time
import urllib.parse
import pg8000.dbapi as pg


def _connect(dsn: str, autocommit: bool):
    u = urllib.parse.urlparse(dsn)
    c = pg.connect(user=urllib.parse.unquote(u.username or "postgres"), password=urllib.parse.unquote(u.password) if u.password else None,
                   host=u.hostname or "127.0.0.1", port=u.port or 5432, database=(u.path or "/postgres")[1:], timeout=10)
    c.autocommit = autocommit
    return c


class _Conn:
    """Closes the connection at the end of a `with` block (pg8000's connection does not)."""
    def __init__(self, c): self.c = c
    def __enter__(self): return self.c
    def __exit__(self, *a): self.c.close()


def _q(c, sql, params=()):
    cur = c.cursor()
    cur.execute(sql, params)
    return cur

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags(id SERIAL PRIMARY KEY, tag TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS raw(tag_id INTEGER NOT NULL, ts BIGINT NOT NULL, v DOUBLE PRECISION, s INTEGER, q TEXT,
                               PRIMARY KEY(tag_id, ts));
CREATE TABLE IF NOT EXISTS agg1m(tag_id INTEGER NOT NULL, bucket BIGINT NOT NULL, n INTEGER NOT NULL,
                                 vmin DOUBLE PRECISION, vmax DOUBLE PRECISION, vsum DOUBLE PRECISION, vlast DOUBLE PRECISION,
                                 PRIMARY KEY(tag_id, bucket));
CREATE INDEX IF NOT EXISTS raw_ts ON raw(ts);
"""


def sql(dsn: str, query: str, params=()):
    """One-off statement on a fresh connection (tests and tools); returns the rows, or [] for statements without a result set."""
    with _Conn(_connect(dsn, True)) as c:
        cur = _q(c, query, params)
        return cur.fetchall() if cur.description else []


class PgStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.w = _connect(dsn, True)
        for stmt in [x for x in SCHEMA.split(";") if x.strip()]:
            _q(self.w, stmt)
        self.timescale = False
        try:
            if _q(self.w, "SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'").fetchone():
                _q(self.w, "CREATE EXTENSION IF NOT EXISTS timescaledb")
                # ts is epoch milliseconds (BIGINT): integer-time hypertable, 7 days per chunk
                _q(self.w, "SELECT create_hypertable('raw','ts',chunk_time_interval=>604800000,if_not_exists=>TRUE,migrate_data=>TRUE)")
                self.timescale = True
        except Exception:
            self.timescale = False
        self.lock = threading.Lock()
        self.ids: dict[str, int] = {t: i for t, i in _q(self.w, "SELECT tag, id FROM tags").fetchall()}
        self.rows_written = 0
        self.t0 = time.time()

    def _id(self, tag: str) -> int:
        i = self.ids.get(tag)
        if i is None:
            _q(self.w, "INSERT INTO tags(tag) VALUES(%s) ON CONFLICT DO NOTHING", (tag,))
            i = _q(self.w, "SELECT id FROM tags WHERE tag=%s", (tag,)).fetchone()[0]
            self.ids[tag] = i
        return i

    def write(self, pts: list[dict]) -> int:
        """Set-based: one INSERT ... SELECT unnest(...) for the raw rows (duplicates skipped, only new rows returned) and one for
        the aggregate built from exactly those new rows, so a redelivered batch changes nothing."""
        if not pts:
            return 0
        tids, tss, vs, ss, qs = [], [], [], [], []
        for p in pts:
            tids.append(self._id(p["t"])); tss.append(int(p["m"])); vs.append(p.get("v")); ss.append(p.get("s")); qs.append(p.get("q"))
        with self.lock:
            self.w.autocommit = False
            try:
                rows = _q(self.w,
                    "INSERT INTO raw(tag_id,ts,v,s,q) SELECT * FROM unnest(%s::int[],%s::bigint[],%s::float8[],%s::int[],%s::text[]) "
                    "ON CONFLICT DO NOTHING RETURNING tag_id, ts, v, s", (tids, tss, vs, ss, qs)).fetchall()
                agg: dict[tuple[int, int], list] = {}
                for tid, ts, v, s_ in rows:
                    x = v if v is not None else (float(s_) if s_ is not None else None)
                    if x is None:
                        continue
                    k = (tid, ts // 60000 * 60000)
                    a = agg.get(k)
                    if a is None:
                        agg[k] = [1, x, x, x, x, ts]
                    else:
                        a[0] += 1; a[1] = min(a[1], x); a[2] = max(a[2], x); a[3] += x
                        if ts >= a[5]:
                            a[4], a[5] = x, ts
                if agg:
                    ks = list(agg)
                    _q(self.w,
                        "INSERT INTO agg1m(tag_id,bucket,n,vmin,vmax,vsum,vlast) SELECT * FROM unnest(%s::int[],%s::bigint[],%s::int[],%s::float8[],%s::float8[],%s::float8[],%s::float8[]) "
                        "ON CONFLICT(tag_id,bucket) DO UPDATE SET n=agg1m.n+EXCLUDED.n, vmin=LEAST(agg1m.vmin,EXCLUDED.vmin), "
                        "vmax=GREATEST(agg1m.vmax,EXCLUDED.vmax), vsum=agg1m.vsum+EXCLUDED.vsum, vlast=EXCLUDED.vlast",
                        ([k[0] for k in ks], [k[1] for k in ks], [agg[k][0] for k in ks], [agg[k][1] for k in ks], [agg[k][2] for k in ks],
                         [agg[k][3] for k in ks], [agg[k][4] for k in ks]))
                self.w.commit()
            except Exception:
                self.w.rollback()
                raise
            finally:
                self.w.autocommit = True
            self.rows_written += len(rows)
            return len(rows)

    def _read(self):
        return _Conn(_connect(self.dsn, True))

    def trend(self, tag: str, t_from: int, t_to: int, max_points: int = 600) -> dict:
        with self._read() as c:
            r = _q(c, "SELECT id FROM tags WHERE tag=%s", (tag,)).fetchone()
            if not r:
                return {"tag": tag, "resolution": "none", "points": []}
            tid = r[0]
            if t_to - t_from <= 15 * 60_000:
                rows = _q(c, "SELECT ts, v, s FROM raw WHERE tag_id=%s AND ts BETWEEN %s AND %s ORDER BY ts", (tid, t_from, t_to)).fetchall()
                pts = [{"t": ts, "min": v if v is not None else s, "avg": v if v is not None else s, "max": v if v is not None else s, "n": 1} for ts, v, s in rows]
                res = "raw"
            else:
                rows = _q(c, "SELECT bucket, n, vmin, vmax, vsum FROM agg1m WHERE tag_id=%s AND bucket BETWEEN %s AND %s ORDER BY bucket", (tid, t_from, t_to)).fetchall()
                pts = [{"t": b, "min": lo, "avg": sm / n, "max": hi, "n": n} for b, n, lo, hi, sm in rows]
                res = "1m"
        if len(pts) > max_points:
            k = -(-len(pts) // max_points)
            merged = []
            for i in range(0, len(pts), k):
                g = pts[i:i + k]
                n = sum(x["n"] for x in g)
                merged.append({"t": g[0]["t"], "min": min(x["min"] for x in g), "max": max(x["max"] for x in g),
                               "avg": sum(x["avg"] * x["n"] for x in g) / n, "n": n})
            pts = merged
            res += f"/decimated x{k}"
        return {"tag": tag, "resolution": res, "points": pts}

    def series(self, tag: str, t_from: int, t_to: int):
        with self._read() as c:
            r = _q(c, "SELECT id FROM tags WHERE tag=%s", (tag,)).fetchone()
            if not r:
                return [], []
            rows = _q(c, "SELECT bucket, vsum / n FROM agg1m WHERE tag_id=%s AND bucket BETWEEN %s AND %s ORDER BY bucket", (r[0], t_from, t_to)).fetchall()
        return [a for a, _ in rows], [b for _, b in rows]

    def purge_raw(self, before_ms: int) -> int:
        with self.lock:
            return _q(self.w, "DELETE FROM raw WHERE ts < %s", (before_ms,)).rowcount

    def stats(self) -> dict:
        with self._read() as c:
            raw = _q(c, "SELECT COUNT(*), MIN(ts), MAX(ts) FROM raw").fetchone()
            agg = _q(c, "SELECT COUNT(*) FROM agg1m").fetchone()[0]
            size = _q(c, "SELECT pg_total_relation_size('raw') + pg_total_relation_size('agg1m')").fetchone()[0]
        return {"raw_rows": raw[0], "oldest_ms": raw[1], "newest_ms": raw[2], "agg1m_rows": agg, "tags": len(self.ids),
                "db_bytes": int(size), "bytes_per_raw_row": round(size / max(raw[0], 1), 1), "backend": "timescaledb" if self.timescale else "postgresql",
                "rows_written_since_start": self.rows_written, "uptime_s": round(time.time() - self.t0, 1),
                "avg_rows_per_s": round(self.rows_written / max(time.time() - self.t0, 1), 1)}
