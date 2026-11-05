"""SQLite time-series store (stand-in for TimescaleDB): raw change log + 1-minute continuous aggregate.

Design notes
- WAL + synchronous=NORMAL; one writer thread, readers use their own connections.
- Aggregates are maintained incrementally at write time (the equivalent of a continuous aggregate).
- Tag names are interned to small integers to keep rows compact.
"""
from __future__ import annotations
import sqlite3, threading, time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, tag TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS raw(tag_id INTEGER NOT NULL, ts INTEGER NOT NULL, v REAL, s INTEGER, q TEXT,
                               PRIMARY KEY(tag_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg1m(tag_id INTEGER NOT NULL, bucket INTEGER NOT NULL, n INTEGER NOT NULL,
                                 vmin REAL, vmax REAL, vsum REAL, vlast REAL,
                                 PRIMARY KEY(tag_id, bucket)) WITHOUT ROWID;
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.w = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.w.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.ids: dict[str, int] = {t: i for i, t in self.w.execute("SELECT id, tag FROM tags")}
        self.rows_written = 0
        self.t0 = time.time()

    def _id(self, tag: str) -> int:
        i = self.ids.get(tag)
        if i is None:
            cur = self.w.execute("INSERT OR IGNORE INTO tags(tag) VALUES(?)", (tag,))
            i = self.w.execute("SELECT id FROM tags WHERE tag=?", (tag,)).fetchone()[0]
            self.ids[tag] = i
        return i

    def write(self, pts: list[dict]) -> int:
        """pts: [{t, m(ts ms), v|s, q?}] -> rows written."""
        if not pts:
            return 0
        with self.lock:
            self.w.execute("BEGIN")
            n = 0
            for p in pts:
                tid = self._id(p["t"])
                ts = int(p["m"])
                v = p.get("v")
                s = p.get("s")
                self.w.execute("INSERT OR REPLACE INTO raw(tag_id,ts,v,s,q) VALUES(?,?,?,?,?)", (tid, ts, v, s, p.get("q")))
                n += 1
                x = v if v is not None else (float(s) if s is not None else None)
                if x is not None:
                    b = ts // 60000 * 60000
                    self.w.execute(
                        "INSERT INTO agg1m(tag_id,bucket,n,vmin,vmax,vsum,vlast) VALUES(?,?,1,?,?,?,?) "
                        "ON CONFLICT(tag_id,bucket) DO UPDATE SET n=n+1, vmin=MIN(vmin,excluded.vmin), vmax=MAX(vmax,excluded.vmax), "
                        "vsum=vsum+excluded.vsum, vlast=excluded.vlast", (tid, b, x, x, x, x))
            self.w.execute("COMMIT")
            self.rows_written += n
            return n

    def reader(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        return c

    def trend(self, tag: str, t_from: int, t_to: int, max_points: int = 600) -> dict:
        """Raw points for short ranges, 1-minute aggregates otherwise; always min/avg/max per returned point."""
        c = self.reader()
        try:
            r = c.execute("SELECT id FROM tags WHERE tag=?", (tag,)).fetchone()
            if not r:
                return {"tag": tag, "resolution": "none", "points": []}
            tid = r[0]
            span = t_to - t_from
            if span <= 15 * 60_000:
                rows = c.execute("SELECT ts, v, s FROM raw WHERE tag_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (tid, t_from, t_to)).fetchall()
                pts = [{"t": ts, "min": v if v is not None else s, "avg": v if v is not None else s, "max": v if v is not None else s, "n": 1} for ts, v, s in rows]
                res = "raw"
            else:
                rows = c.execute("SELECT bucket, n, vmin, vmax, vsum FROM agg1m WHERE tag_id=? AND bucket BETWEEN ? AND ? ORDER BY bucket", (tid, t_from, t_to)).fetchall()
                pts = [{"t": b, "min": lo, "avg": sm / n, "max": hi, "n": n} for b, n, lo, hi, sm in rows]
                res = "1m"
            if len(pts) > max_points:                 # decimate by merging neighbours (keeps min/max envelope)
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
        finally:
            c.close()

    def series(self, tag: str, t_from: int, t_to: int):
        """Undecimated 1-minute average series (ms, value) for forecasting."""
        c = self.reader()
        try:
            r = c.execute("SELECT id FROM tags WHERE tag=?", (tag,)).fetchone()
            if not r:
                return [], []
            rows = c.execute("SELECT bucket, vsum / n FROM agg1m WHERE tag_id=? AND bucket BETWEEN ? AND ? ORDER BY bucket", (r[0], t_from, t_to)).fetchall()
        finally:
            c.close()
        return [a for a, _ in rows], [b for _, b in rows]

    def stats(self) -> dict:
        c = self.reader()
        try:
            raw = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM raw").fetchone()
            agg = c.execute("SELECT COUNT(*) FROM agg1m").fetchone()[0]
        finally:
            c.close()
        import os
        size = os.path.getsize(self.path) + (os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0)
        return {"backend": "sqlite", "raw_rows": raw[0], "oldest_ms": raw[1], "newest_ms": raw[2], "agg1m_rows": agg, "tags": len(self.ids),
                "db_bytes": size, "bytes_per_raw_row": round(size / max(raw[0], 1), 1),
                "rows_written_since_start": self.rows_written, "uptime_s": round(time.time() - self.t0, 1),
                "avg_rows_per_s": round(self.rows_written / max(time.time() - self.t0, 1), 1)}
