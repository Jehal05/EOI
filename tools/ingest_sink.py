#!/usr/bin/env python3
"""Throwaway NDJSON sink standing in for scada-core while verifying the FEP path (test tool only)."""
import json, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
N = {"batches": 0, "lines": 0, "bad": 0, "tags": set(), "lat": []}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        d = {"batches": N["batches"], "lines": N["lines"], "bad": N["bad"], "distinct_tags": len(N["tags"]),
             "lat_ms_p50": sorted(N["lat"])[len(N["lat"])//2] if N["lat"] else None,
             "lat_ms_max": max(N["lat"]) if N["lat"] else None}
        b = json.dumps(d).encode(); self.send_response(200); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(401); self.end_headers(); return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        now = time.time()
        for ln in body.splitlines():
            try:
                o = json.loads(ln); N["lines"] += 1; N["tags"].add(o["tag"])
                from datetime import datetime
                rx = datetime.fromisoformat(o["rx"].replace("Z", "+00:00")).timestamp()
                N["lat"].append((now - rx) * 1000)
            except Exception: N["bad"] += 1
        N["batches"] += 1; self.send_response(200); self.end_headers()
ThreadingHTTPServer(("127.0.0.1", 8083), H).serve_forever()
