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

Usage: landing_check.py

Prints one line per check. Exit 0 when every check passes, 4 when any
definite negative stands after the retry (the reasons are on stdout),
and 0 with a note when a check cannot be formed, since doubt proceeds.
"""
import configparser
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlparse

TIMEOUT_S = 15
RETRY_AFTER_S = 10


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

    if not formed:
        print("landing: nothing to check; proceeding")
        return 0
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
