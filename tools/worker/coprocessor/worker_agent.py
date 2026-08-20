#!/usr/bin/env python3
"""Worker agent for the coprocessor workflow.

Owns all networking on the worker side, per docs/WORK_UNIT_CONTRACT.md:
pulls unit specs from the dispatcher and stages them atomically into the
shared work directory's inbox, keeping it a configured depth ahead of the
executable; collects completed unit directories from the outbox and posts
hits and metadata back. The executable never touches the network.

Usage:
  worker_agent.py --dispatcher http://host:8750 --work WORKDIR
                  [--depth 3] [--poll 2] [--worker ID] [--keep-sent]
"""
import argparse
import json
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def log(msg):
    print(f"{ts()} {msg}", flush=True)


class Agent:

    def __init__(self, dispatcher, work, depth, worker, keep_sent):
        self.base = dispatcher.rstrip("/")
        self.inbox = work / "inbox"
        self.outbox = work / "outbox"
        self.sent = work / "sent"
        self.depth = depth
        self.worker = worker
        self.keep_sent = keep_sent
        for d in (self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)
        if keep_sent:
            self.sent.mkdir(parents=True, exist_ok=True)
        self.bytes_up = 0
        self.bytes_down = 0

    def _request(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers or {})
        return urllib.request.urlopen(req, timeout=60)

    # ---- outbox -> dispatcher ----

    def collect(self):
        """Upload every completed unit directory, then remove (or archive) it."""
        for udir in sorted(self.outbox.iterdir()):
            if not udir.is_dir():
                continue
            uid = udir.name
            done = (udir / "done").exists()
            error = (udir / "error.json").exists()
            if not (done or error):
                continue                       # in flight
            try:
                self._upload(uid, udir, error)
            except (urllib.error.URLError, OSError) as e:
                log(f"ERROR upload {uid} failed, will retry: {e}")
                continue
            if self.keep_sent:
                shutil.move(str(udir), str(self.sent / uid))
            else:
                shutil.rmtree(udir)

    def _upload(self, uid, udir, error):
        hits = udir / "hits.npy"
        if hits.exists():
            data = hits.read_bytes()
            with self._request("PUT", f"/result/{uid}/hits.npy", data=data,
                               headers={"Content-Type": "application/octet-stream"}) as r:
                r.read()
            self.bytes_up += len(data)
            log(f"uploaded {uid} hits.npy {len(data)} bytes")
        record_path = udir / ("error.json" if error else "unit.json")
        try:
            record = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f"ERROR {uid}: unreadable {record_path.name} ({e}); reporting as error")
            record = {"contract_version": 1, "unit_id": uid, "status": "error",
                      "failures": [{"stage": "agent", "message": f"unreadable record: {e}"}]}
        if error:
            record.setdefault("status", "error")
        body = json.dumps(record).encode()
        with self._request("POST", f"/result/{uid}", data=body,
                           headers={"Content-Type": "application/json"}) as r:
            r.read()
        self.bytes_up += len(body)
        log(f"posted {uid} record status={record.get('status')}")

    # ---- dispatcher -> inbox ----

    def top_up(self):
        buffered = len(list(self.inbox.glob("*.unit.json")))
        while buffered < self.depth:
            try:
                with self._request("GET", f"/unit?worker={self.worker}") as r:
                    if r.status == 204:
                        return
                    data = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 204:
                    return
                raise
            spec = json.loads(data)
            self.bytes_down += len(data)
            uid = spec["unit_id"]
            tmp = self.inbox / f".{uid}.tmp"
            tmp.write_text(json.dumps(spec, indent=1))
            tmp.rename(self.inbox / f"{uid}.unit.json")
            log(f"staged {uid} ({len(data)} bytes)")
            buffered += 1

    def run(self, poll):
        log(f"agent {self.worker} -> {self.base}, inbox depth {self.depth}")
        while True:
            try:
                self.collect()
                self.top_up()
            except (urllib.error.URLError, OSError) as e:
                log(f"ERROR dispatcher unreachable, retrying: {e}")
            time.sleep(poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dispatcher", required=True)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--worker", default=socket.gethostname())
    ap.add_argument("--keep-sent", action="store_true",
                    help="archive uploaded unit dirs under sent/ instead of deleting")
    args = ap.parse_args()
    Agent(args.dispatcher, args.work, args.depth, args.worker,
          args.keep_sent).run(args.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
