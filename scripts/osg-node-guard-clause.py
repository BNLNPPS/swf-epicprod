#!/usr/bin/env python3
"""The node guard's exclusion as matchmaking on the OSG pool.

The node guard (site-canary docs/NODE_GUARD.md) publishes the nodes it judges
black holes every cycle, in a document that is live for twenty minutes. The
payload's landing check reads it and declines a job on an excluded node, but
on the OSG pool the node's pilots go on taking jobs, each declined at the cost
of an attempt (2026-10-20: one CNAF node declined 361 jobs in four hours after
the guard excluded it). This keeps the pilots off the node: it writes the
OSG queues' black holes into the submit descriptions as the job attribute
EpicprodNodeGuard, an expression that is false on an excluded node, which
each description's Requirements references
(`MY.EpicprodNodeGuard =!= False`). Harvester reads a description at every
submission, so the next pilot submitted is never matched to the node
(docs/OSG_SUBMISSION.md, Excluding what delivers nothing).

Only the marker line and the line after it are written; the rest of the
description, the operators' own Requirements clause included, is left as it
is. A node is named by its full host name, or by its bare name together with
its glidein site, as the pool advertises both forms. Every node excluded on an
OSG queue is excluded on all the descriptions given, since the pool and the
payload are the same for the OSG queues. A document that is not live, or has
expired, writes True; when it cannot be read, the clause stands until the
validity it was written with has passed and is then written True, so a
publisher that stops leaves no exclusion standing.

The clause reaches only new matches. A pilot already running on a node when
the guard excludes it keeps taking jobs that the payload's landing check
declines in seconds, each at the cost of an attempt (23 October 2026: four
pilots on nod46 at UConn, one declined job every two minutes each). With
--remove-pilots the script also removes this user's running pilots on the
excluded nodes (condor_rm; a job in flight there is one the landing check
would decline), and logs each removal.

Usage: osg-node-guard-clause.py SDF [SDF ...] [--url URL] [--queues PREFIX]
                                [--remove-pilots] [--dry-run]
Runs under Python 3.6 (the submit host's), standard library only.
"""

import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request

URL = "https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/node-exclusion.json"
QUEUES = "BNL_OSG_"
MARKER = "# epicprod node guard: osg-node-guard-clause.py writes the next line"
ATTRIBUTE = "+EpicprodNodeGuard"
HOST = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$")
SITE = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_time(text):
    """An ISO 8601 UTC time as the guard writes it, in Python 3.6."""
    text = re.sub(r"([+-]\d\d):(\d\d)$", r"\1\2", text.replace("Z", "+00:00"))
    fmt = "%Y-%m-%dT%H:%M:%S.%f%z" if "." in text else "%Y-%m-%dT%H:%M:%S%z"
    return datetime.datetime.strptime(text, fmt)


def excluded_nodes(document, now, queues=QUEUES):
    """The (host, site) pairs the document excludes now on the queues whose
    names start with the prefix; none unless the document is live and valid."""
    if document.get("mode") != "live" or parse_time(document["valid_until"]) <= now:
        return []
    nodes = set()
    for node in document.get("nodes") or []:
        if not str(node.get("queue") or "").startswith(queues):
            continue
        if node.get("until") and parse_time(node["until"]) <= now:
            continue
        nodes.add((str(node.get("host") or ""), str(node.get("site") or "")))
    return sorted(nodes)


def clause(nodes):
    """The attribute's expression: False on any of the nodes, else True.
    Returns the expression and the nodes it could not name safely."""
    terms, skipped = [], []
    for host, site in nodes:
        name = host.replace(".", "[.]")
        if not HOST.match(host):
            skipped.append((host, site))
        elif "." in host:
            terms.append('regexp("^%s$", TARGET.Machine) =?= True' % name)
        elif site and SITE.match(site):
            terms.append('(TARGET.GLIDEIN_Site =?= "%s" && regexp("^%s([.]|$)", TARGET.Machine) =?= True)'
                         % (site, name))
        else:
            skipped.append((host, site))
    return ("!( %s )" % " || ".join(terms) if terms else "True"), skipped


def rewrite(text, expression, valid_until):
    """The description with the marker and attribute lines replaced."""
    lines = text.split("\n")
    marks = [i for i, line in enumerate(lines) if line.startswith(MARKER)]
    if len(marks) != 1 or marks[0] + 1 >= len(lines) or not lines[marks[0] + 1].startswith(ATTRIBUTE):
        raise ValueError("no single marker line followed by %s" % ATTRIBUTE)
    i = marks[0]
    lines[i] = "%s; valid until %s" % (MARKER, valid_until)
    lines[i + 1] = "%s = %s" % (ATTRIBUTE, expression)
    return "\n".join(lines)


def written_validity(text):
    """The validity the description's clause was written with, or None."""
    for line in text.split("\n"):
        if line.startswith(MARKER) and "; valid until " in line:
            try:
                return parse_time(line.split("; valid until ", 1)[1].strip())
            except ValueError:
                return None
    return None


def write(path, text):
    """Replace the file atomically, keeping its mode."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".sdf")
    with os.fdopen(handle, "w") as out:
        out.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def pilots_on(nodes, rows):
    """The running pilots that sit on an excluded node: rows are
    (cluster, proc, machine, site) as condor_q reports them, nodes the
    (host, site) pairs the document excludes. A full host name matches the
    machine exactly; a bare name matches the machine's first label on the
    same glidein site. Pure."""
    full = {h.lower() for h, _ in nodes if "." in h}
    bare = {(h.lower(), s) for h, s in nodes if "." not in h and s}
    hits = []
    for cluster, proc, machine, site in rows:
        m = (machine or "").lower()
        if m in full or (m.split(".", 1)[0], site) in bare:
            hits.append((cluster, proc, machine, site))
    return hits


def running_pilots(queues):
    """(cluster, proc, machine, site) for this user's running pilots whose
    arguments name a queue with the prefix."""
    out = subprocess.run(["condor_q", "-constraint", "JobStatus == 2", "-af:t", "ClusterId", "ProcId",
                          "RemoteHost", "MachineAttrGLIDEIN_Site0", "Args"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("condor_q failed: %s" % out.stderr.strip())
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or ("-q " + queues) not in parts[4]:
            continue
        machine = parts[2].rsplit("@", 1)[-1]
        rows.append((parts[0], parts[1], machine, "" if parts[3] == "undefined" else parts[3]))
    return rows


def remove_pilots(nodes, queues, stamp, dry_run):
    """Remove the running pilots on the excluded nodes; print each one."""
    hits = pilots_on(nodes, running_pilots(queues))
    for cluster, proc, machine, site in hits:
        job = "%s.%s" % (cluster, proc)
        if dry_run:
            print("%s would remove %s on excluded node %s (%s)" % (stamp, job, machine, site))
            continue
        rm = subprocess.run(["condor_rm", job], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=60)
        print("%s removed %s on excluded node %s (%s): %s" % (stamp, job, machine, site,
              (rm.stdout + rm.stderr).strip() or "rc %d" % rm.returncode))
    return hits


def _option(args, name, default):
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return default


def main(argv):
    args = argv[1:]
    url = _option(args, "--url", URL)
    queues = _option(args, "--queues", QUEUES)
    remove = "--remove-pilots" in args
    dry_run = "--dry-run" in args
    args = [a for a in args if a not in ("--remove-pilots", "--dry-run")]
    if not args:
        sys.exit(__doc__)
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            document = json.loads(response.read().decode())
        valid_until = parse_time(document["valid_until"])
        nodes = excluded_nodes(document, now, queues)
        error = None
    except Exception as exc:  # noqa: BLE001
        document, nodes, valid_until, error = None, None, None, exc
    expression, skipped = clause(nodes or [])
    status = 0
    for path in args:
        with open(path) as f:
            text = f.read()
        if error is not None:
            written = written_validity(text)
            if written is not None and written > now:
                print("%s %s: exclusion not read (%s); clause left as written, valid until %s"
                      % (stamp, path, error, written.isoformat()))
                status = 1
                continue
            print("%s %s: exclusion not read (%s); written True" % (stamp, path, error))
            status = 1
        new = rewrite(text, expression, (valid_until or now).isoformat())
        if new != text:
            write(path, new)
    if error is None:
        named = ", ".join(host for host, _ in nodes) or "none"
        print("%s mode %s, valid until %s; excluded on %s*: %s; written to %d descriptions"
              % (stamp, document.get("mode"), valid_until.isoformat(), queues, named, len(args)))
    if skipped:
        print("%s not named (bare host without a site, or not a host name): %s" % (stamp, skipped))
        status = 1
    if remove and error is None and nodes:
        try:
            remove_pilots(nodes, queues, stamp, dry_run)
        except Exception as exc:  # noqa: BLE001
            print("%s running pilots not checked (%s)" % (stamp, exc))
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
