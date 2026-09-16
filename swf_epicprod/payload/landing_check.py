#!/usr/bin/env python3
"""Whether this worker can reach what the job needs, before any work.

Run by run.sh in the payload's first seconds. A job that cannot reach
the catalog of record does the physics and then dies at registration,
hours later, with nothing delivered; a job that cannot reach the input
door dies in minutes. Either is a definite negative that costs seconds
to detect, so the payload declines the landing instead of running into
it (site-canary DESIGN.md, the carrier that declines its landing;
swf-epicprod docs/EPICPROD_PAYLOAD.md, exit code 80).

Checks, each a TCP connect and, for the catalog, a TLS handshake, with
a short timeout and one retry:
  the Rucio server, from ``auth_host`` in RUCIO_CONFIG, over TLS
  the input door, from XRDRURL (root://host:port), TCP only

And the node guard's exclusion (site-canary docs/NODE_GUARD.md,
Actuation): the document the guard publishes on the devcloud bucket's
public pilot prefix (NODE_EXCLUSION_URL), fetched once with a short
timeout. This worker is excluded when the document is live, inside its
validity, and lists this host (by its full name, or by its bare name
when the record holds a bare name) on this queue when the queue is
known here (PILOT_SITENAME). A shadow document only says what it would
do. A document that cannot be fetched or read proceeds.

Usage: landing_check.py

Prints one line per check. Exit 0 when every check passes, 4 when any
definite negative stands after the retry (the reasons are on stdout),
and 0 with a note when a check cannot be formed, since doubt proceeds.
"""
import configparser
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

TIMEOUT_S = 15
RETRY_AFTER_S = 10
EXCLUSION_URL = os.environ.get(
    "NODE_EXCLUSION_URL",
    "https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/node-exclusion.json")
EXCLUSION_TIMEOUT_S = 10


def _host_names():
    """This worker's names as the job record may hold them: the full
    name and the bare one."""
    names = set()
    for fn in (socket.gethostname, socket.getfqdn):
        try:
            n = (fn() or "").strip().lower()
        except OSError:
            continue
        if n:
            names.add(n)
            names.add(n.split(".", 1)[0])
    return names


def excluded_here(document, names, queue, now):
    """The matching entry when this worker is excluded by a live document
    in force, else None. Pure: the document, the names, the queue and the
    clock come in."""
    if not isinstance(document, dict) or document.get("mode") != "live":
        return None
    try:
        until = datetime.fromisoformat(str(document.get("valid_until", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if now > until:
        return None
    full = {n for n in names if "." in n}
    bare = {n.split(".", 1)[0] for n in names}
    for entry in document.get("nodes") or []:
        host = str(entry.get("host") or "").strip().lower()
        if not host:
            continue
        if queue and entry.get("queue") and entry["queue"] != queue:
            continue
        if host in full or host in names or ("." not in host and host in bare):
            return entry
    return None


def check_exclusion():
    """Fetch the published exclusion and read it for this worker; print
    the outcome. True when the landing may proceed."""
    try:
        with urllib.request.urlopen(EXCLUSION_URL, timeout=EXCLUSION_TIMEOUT_S) as r:
            document = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"landing exclusion: not read ({exc.__class__.__name__}: {exc}); proceeding")
        return True
    names = _host_names()
    queue = os.environ.get("PILOT_SITENAME", "").strip()
    now = datetime.now(timezone.utc)
    entry = excluded_here(document, names, queue, now)
    if entry is not None:
        print(f"landing exclusion FAILED: this node {entry['host']} on {entry['queue']} is excluded "
              f"by the node guard since {entry.get('since')} ({entry.get('reason')}); "
              f"declining the landing")
        return False
    shadow = excluded_here(dict(document, mode="live"), names, queue, now)
    if document.get("mode") != "live" and shadow is not None:
        print(f"landing exclusion: this node {shadow['host']} is listed by the node guard "
              f"in {document.get('mode')} mode; would decline, proceeding")
    else:
        print(f"landing exclusion: not excluded ({len(document.get('nodes') or [])} nodes listed, "
              f"{document.get('mode')} mode)")
    return True


def _target(url):
    """(host, port) from a URL, or None when it has no host."""
    parsed = urlparse(url if "://" in url else f"//{url}")
    if not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 1094)
    return parsed.hostname, port


def _connect(host, port, tls):
    """One attempt; returns '' on success or the reason on failure."""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as sock:
            if tls:
                context = ssl.create_default_context()
                # Reachability is the question, not the certificate chain
                # on this worker; the Rucio client verifies for real.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                    tls_sock.settimeout(TIMEOUT_S)
                    tls_sock.do_handshake()
        return ""
    except (OSError, ssl.SSLError) as exc:
        elapsed = time.monotonic() - started
        return f"{exc.__class__.__name__}: {exc} after {elapsed:.1f}s"


def check(label, host, port, tls):
    """Connect, retry once, and print the outcome; True when reachable."""
    reason = _connect(host, port, tls)
    if reason:
        time.sleep(RETRY_AFTER_S)
        reason = _connect(host, port, tls)
    what = "tls" if tls else "tcp"
    if reason:
        print(f"landing {label} {host}:{port} {what} FAILED twice: {reason}")
        return False
    print(f"landing {label} {host}:{port} {what} ok")
    return True


def main():
    ok = True
    formed = 0

    config_path = os.environ.get("RUCIO_CONFIG", "")
    auth_host = ""
    if config_path:
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path)
            auth_host = (parser.get("client", "auth_host", fallback="")
                         or parser.get("client", "rucio_host", fallback="")).strip()
        except configparser.Error as exc:
            print(f"landing catalog: RUCIO_CONFIG unreadable ({exc}); not checked")
    target = _target(auth_host) if auth_host else None
    if target:
        formed += 1
        ok = check("catalog", target[0], target[1], tls=True) and ok
    else:
        print("landing catalog: no auth_host in RUCIO_CONFIG; not checked")

    door = os.environ.get("XRDRURL", "").strip()
    target = _target(door) if door else None
    if target:
        formed += 1
        ok = check("input-door", target[0], target[1], tls=False) and ok
    else:
        print("landing input-door: no XRDRURL; not checked")

    # The node guard's exclusion, whatever the reachability checks found:
    # an excluded node is a definite negative of its own.
    excluded_ok = check_exclusion()

    if not formed and excluded_ok:
        print("landing: no reachability check could be formed; proceeding")
        return 0
    return 0 if (ok and excluded_ok) else 4


if __name__ == "__main__":
    sys.exit(main())
