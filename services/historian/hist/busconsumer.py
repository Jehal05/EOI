"""Consumes telemetry from a Redis Streams consumer group (at-least-once; the store's writes are idempotent).

Several historians can share one group name to split the load, or use different names to each get every message.
Messages are acknowledged only after the store has committed them. Entries left pending by a crashed consumer are
re-read first (id "0") on start."""
from __future__ import annotations
import json, os, threading, time
import redis


def run(store, state, url: str, stream: str = "gs:telemetry", group: str = "historian", consumer: str | None = None):
    consumer = consumer or f"h-{os.getpid()}"
    r = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=2, decode_responses=True)
    backoff = 0.5
    while True:
        try:
            try:
                r.xgroup_create(stream, group, id="0", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            cursor = "0"                                  # first drain our own pending entries, then switch to new ones
            state["connected_to"] = "bus:" + stream
            backoff = 0.5
            while True:
                res = r.xreadgroup(group, consumer, {stream: cursor}, count=200, block=1000)
                entries = res[0][1] if res else []
                if cursor == "0" and not entries:
                    cursor = ">"
                    continue
                ids = []
                for eid, f in entries:
                    try:
                        m = json.loads(f["d"])
                        if m.get("pts"):
                            store.write(m["pts"])
                            state["batches"] += 1
                    except Exception as e:                # a poison message must not block the stream: record and acknowledge
                        state["last_error"] = f"bus message {eid}: {type(e).__name__}"
                    ids.append(eid)
                if ids:
                    r.xack(stream, group, *ids)
        except Exception as e:
            state["last_error"] = f"{type(e).__name__}: {e}"
            state["connected_to"] = None
            state["reconnects"] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 5)
