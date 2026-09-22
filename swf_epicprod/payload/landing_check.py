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

And the write door the job's outputs must pass, when the job has one
(LANDING_WRITE_DOOR; run.sh sets it when the output RSE is the one
behind that door, so the door is the only way home rather than a
failover). A door whose host certificate has expired takes nothing:
the job runs its physics and dies at registration with the output
lost, which is what epicxrd1's certificate did from 2026-09-20 — tens
of thousands of Perlmutter jobs, twelve minutes of simulation and
reconstruction each, delivering nothing. The check is one ``xrdfs
stat`` at the door, and, when the door does not answer it, the date on
the certificate the door serves, read through openssl. Only a date
already past declines the landing; a door that answers, a certificate
in force, and a certificate that cannot be read all proceed, since
doubt proceeds and declining spends one of the job's attempts.

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
5 when the job's write door refused with an expired certificate (a
condition no other worker escapes, so it is its own code), and 0 with
a note when a check cannot be formed, since doubt proceeds.
"""
import configparser
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

TIMEOUT_S = 15
RETRY_AFTER_S = 10
# The write door probe: an xrdfs stat costs a round trip, and the
# certificate read a handshake.
DOOR_STAT_PATH = "/"
DOOR_TIMEOUT_S = 30
DOOR_CERT_TIMEOUT_S = 20
# What an xrootd answer of the TLS class looks like. The door with the
# expired certificate answered "[FATAL] TLS error: resource temporarily
# unavailable: Unable to connect to epicxrd1.sdcc.bnl.gov; error_ssl
# (destination)" — "temporarily" notwithstanding, the class is TLS.
TLS_ANSWER = re.compile(r"error_ssl|tls error|ssl error|handshake", re.I)
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


def door_answer_class(rc, text):
    """One xrdfs answer as a class: 'ok', 'tls' or 'other'. Pure."""
    if rc == 0:
        return "ok"
    return "tls" if TLS_ANSWER.search(text or "") else "other"


def certificate_expiry(enddate_text):
    """The notAfter openssl printed ("notAfter=Sep 20 23:59:59 2026 GMT")
    as an aware datetime, or None when the text does not carry one. Pure."""
    match = re.search(r"notAfter=(.+)", enddate_text or "")
    if not match:
        return None
    stamp = match.group(1).strip()
    if stamp.upper().endswith(" GMT"):
        stamp = stamp[:-4].strip()
    try:
        return datetime.strptime(stamp, "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def write_door_verdict(stat_ok, expiry, now):
    """'refused' when the door did not answer and the certificate it
    serves had already expired, else 'proceed'. Pure: the stat's outcome,
    the certificate's notAfter and the clock come in.

    The certificate decides, not the way the client complained. An
    xrdfs refusal reads differently from one client, credential and
    version to the next — the pilot's copy reported "[FATAL] TLS error
    ... error_ssl", a client without a proxy times out at the same door
    — while the date the door serves is the same fact for everyone, and
    an expired one refuses every TLS session the production client will
    open. Declining spends one of the job's attempts, so nothing short
    of that date, read and past, refuses a landing."""
    if stat_ok:
        return "proceed"
    if expiry is None or expiry > now:
        return "proceed"
    return "refused"


def _run(command, timeout, stdin_text=None):
    """(rc, text) from a command: rc 124 when it times out, 127 when it
    is not on the path, so neither is mistaken for an answer."""
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, input=stdin_text)
    except subprocess.TimeoutExpired:
        return 124, f"no answer in {timeout}s"
    except OSError as exc:
        return 127, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, f"{proc.stdout}{proc.stderr}"


def _door_expiry(host, port):
    """The door's leaf certificate notAfter, read with openssl, or None
    when openssl is absent, the door serves nothing, or the date is
    unreadable."""
    _, served = _run(["openssl", "s_client", "-connect", f"{host}:{port}",
                      "-servername", host], DOOR_CERT_TIMEOUT_S, stdin_text="")
    leaf = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                     served, re.S)
    if not leaf:
        return None
    rc, ends = _run(["openssl", "x509", "-noout", "-enddate"],
                    DOOR_CERT_TIMEOUT_S, stdin_text=leaf.group(0))
    return certificate_expiry(ends) if rc == 0 else None


def check_write_door():
    """Stat the write door, twice on a TLS answer, and read its
    certificate when both answers are of that class; print the outcome.
    True when the landing may proceed."""
    door = os.environ.get("LANDING_WRITE_DOOR", "").strip()
    if not door:
        print("landing write-door: this job has none; not checked")
        return True
    target = _target(door)
    if not target:
        print(f"landing write-door: {door} names no host; not checked")
        return True
    host, port = target
    rc, text = _run(["xrdfs", door, "stat", DOOR_STAT_PATH], DOOR_TIMEOUT_S)
    answer = door_answer_class(rc, text)
    said = " ".join((text or "").split())[:200]
    if answer == "ok":
        print(f"landing write-door {host}:{port} ok")
        return True
    expiry = _door_expiry(host, port)
    verdict = write_door_verdict(False, expiry, datetime.now(timezone.utc))
    if verdict == "refused":
        print(f"landing write-door {host}:{port} FAILED: its certificate expired "
              f"{expiry.date().isoformat()} and the door answered {answer}: {said}; "
              f"declining the landing")
        return False
    print(f"landing write-door {host}:{port} answered {answer}, certificate "
          f"{expiry.date().isoformat() if expiry else 'not read'}; proceeding: {said}")
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

    # The write door last, and reported last: a worker-local negative is
    # answered by sending the job elsewhere (4), while an expired door
    # certificate is the same for every worker (5).
    door_ok = check_write_door()

    if not formed:
        print("landing: no reachability check could be formed; proceeding")
    if not (ok and excluded_ok):
        return 4
    if not door_ok:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
