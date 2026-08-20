#!/usr/bin/env python3
"""Work-unit dispatcher v0 for the coprocessor workflow.

Serves work units to worker agents and collects their results, per
docs/WORK_UNIT_CONTRACT.md. Standalone: stdlib only, sqlite state, run
anywhere. The driver enqueues units; agents lease units and post results;
expired leases re-queue.

Endpoints (JSON over HTTP):
  POST /units                 {"units": [spec, ...]} -> {"queued": n, "duplicates": [...]}
  GET  /unit?worker=ID        lease next queued unit -> 200 spec | 204 none
  PUT  /result/<id>/hits.npy  raw bytes -> stored under --results
  POST /result/<id>           unit.json record -> unit done or error
  GET  /status                counts by state, byte totals
  GET  /unit/<id>             stored record (debug)

Usage:
  dispatcher.py --port 8750 --state dispatcher.db --results results/ [--lease-ttl 900]
"""
import argparse
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  unit_id      TEXT PRIMARY KEY,
  spec         TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'queued',
  worker       TEXT,
  lease_expiry REAL,
  result       TEXT,
  hits_bytes   INTEGER NOT NULL DEFAULT 0,
  created      REAL NOT NULL,
  updated      REAL NOT NULL
);
"""

def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

class Store:
    """sqlite state, serialized by a lock (ThreadingHTTPServer workers)."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(SCHEMA)
        self.db.commit()

    def enqueue(self, specs):
        queued, dups = 0, []
        with self.lock:
            now = time.time()
            for spec in specs:
                uid = spec.get("unit_id")
                if not uid:
                    raise ValueError("unit spec without unit_id")
                try:
                    self.db.execute(
                        "INSERT INTO units (unit_id, spec, created, updated) VALUES (?,?,?,?)",
                        (uid, json.dumps(spec), now, now))
                    queued += 1
                except sqlite3.IntegrityError:
                    dups.append(uid)
            self.db.commit()
        return queued, dups

    def lease(self, worker, ttl):
        with self.lock:
            now = time.time()
            expired = self.db.execute(
                "UPDATE units SET state='queued', worker=NULL, lease_expiry=NULL, updated=? "
                "WHERE state='leased' AND lease_expiry < ?", (now, now)).rowcount
            if expired:
                print(f"{ts()} reaped {expired} expired lease(s)", flush=True)
            row = self.db.execute(
                "SELECT unit_id, spec FROM units WHERE state='queued' "
                "ORDER BY created LIMIT 1").fetchone()
            if row is None:
                self.db.commit()
                return None
            uid, spec = row
            self.db.execute(
                "UPDATE units SET state='leased', worker=?, lease_expiry=?, updated=? "
                "WHERE unit_id=?", (worker, now + ttl, now, uid))
            self.db.commit()
        return json.loads(spec)

    def record_hits(self, uid, nbytes):
        with self.lock:
            n = self.db.execute(
                "UPDATE units SET hits_bytes=?, updated=? WHERE unit_id=?",
                (nbytes, time.time(), uid)).rowcount
            self.db.commit()
        return n == 1

    def record_result(self, uid, record):
        state = "done" if record.get("status") == "ok" else "error"
        with self.lock:
            n = self.db.execute(
                "UPDATE units SET state=?, result=?, updated=? WHERE unit_id=?",
                (state, json.dumps(record), time.time(), uid)).rowcount
            self.db.commit()
        return (n == 1), state

    def status(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT state, COUNT(*), SUM(hits_bytes) FROM units GROUP BY state").fetchall()
        out = {"states": {}, "hits_bytes_total": 0}
        for state, count, nbytes in rows:
            out["states"][state] = count
            out["hits_bytes_total"] += nbytes or 0
        return out

    def get(self, uid):
        with self.lock:
            row = self.db.execute(
                "SELECT spec, state, worker, result, hits_bytes FROM units WHERE unit_id=?",
                (uid,)).fetchone()
        if row is None:
            return None
        spec, state, worker, result, hits_bytes = row
        return {"unit_id": uid, "state": state, "worker": worker,
                "spec": json.loads(spec), "hits_bytes": hits_bytes,
                "result": json.loads(result) if result else None}


def make_handler(store, results_dir, lease_ttl):

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, fmt, *args):
            print(f"{ts()} {self.address_string()} {fmt % args}", flush=True)

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/unit":
                worker = dict(p.split("=", 1) for p in query.split("&") if "=" in p
                              ).get("worker", "unknown")
                spec = store.lease(worker, lease_ttl)
                if spec is None:
                    self.send_response(204)
                    self.end_headers()
                else:
                    print(f"{ts()} leased {spec['unit_id']} -> {worker}", flush=True)
                    self._json(200, spec)
            elif path == "/status":
                self._json(200, store.status())
            elif path.startswith("/result/") and path.endswith("/hits.npy"):
                uid = path[len("/result/"):-len("/hits.npy")]
                hits = results_dir / uid / "hits.npy"
                if not hits.is_file():
                    self._json(404, {"error": "no hits stored"})
                    return
                data = hits.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/unit/"):
                rec = store.get(path[len("/unit/"):])
                self._json(200, rec) if rec else self._json(404, {"error": "unknown unit"})
            else:
                self._json(404, {"error": "unknown path"})

        def do_POST(self):
            try:
                if self.path == "/units":
                    req = json.loads(self._body())
                    queued, dups = store.enqueue(req["units"])
                    print(f"{ts()} enqueued {queued}, duplicates {len(dups)}", flush=True)
                    self._json(200, {"queued": queued, "duplicates": dups})
                elif self.path.startswith("/result/"):
                    uid = self.path[len("/result/"):]
                    record = json.loads(self._body())
                    ok, state = store.record_result(uid, record)
                    if not ok:
                        self._json(404, {"error": "unknown unit"})
                        return
                    rdir = results_dir / uid
                    rdir.mkdir(parents=True, exist_ok=True)
                    (rdir / "unit.json").write_text(json.dumps(record, indent=1))
                    print(f"{ts()} result {uid}: {state}", flush=True)
                    self._json(200, {"unit_id": uid, "state": state})
                else:
                    self._json(404, {"error": "unknown path"})
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                self._json(400, {"error": str(e)})

        def do_PUT(self):
            if self.path.startswith("/result/") and self.path.endswith("/hits.npy"):
                uid = self.path[len("/result/"):-len("/hits.npy")]
                data = self._body()
                if not store.record_hits(uid, len(data)):
                    self._json(404, {"error": "unknown unit"})
                    return
                rdir = results_dir / uid
                rdir.mkdir(parents=True, exist_ok=True)
                (rdir / "hits.npy").write_bytes(data)
                print(f"{ts()} hits {uid}: {len(data)} bytes", flush=True)
                self._json(200, {"unit_id": uid, "bytes": len(data)})
            else:
                self._json(404, {"error": "unknown path"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--state", default="dispatcher.db")
    ap.add_argument("--results", default="results")
    ap.add_argument("--lease-ttl", type=float, default=900.0)
    args = ap.parse_args()

    results_dir = Path(args.results)
    results_dir.mkdir(parents=True, exist_ok=True)
    store = Store(args.state)
    server = ThreadingHTTPServer(
        ("", args.port), make_handler(store, results_dir, args.lease_ttl))
    print(f"{ts()} dispatcher on :{args.port} state={args.state} results={results_dir}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
