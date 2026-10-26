#!/usr/bin/env python3
"""Read-only inventory of ePIC's own directories on the GREX CE spool.

Runs as the CE's mapped ePIC account on a GREX batch worker, where the
shared spool is mounted. One JSON line per owned job sandbox, then a
summary: total bytes, the runtime that wrote it (a sandbox holding
pilot-N directories is a pre-79ec244 allocation whose pilots work on the
spool), the allocation's job-intake deadline from its launcher's stdout,
and, per old-runtime sandbox, the live pilots, the PanDA job directories
with their sizes and the log tarballs left beside them. Nothing is
changed. Submitted from pandaharvester01 like the other GREX diagnostics
(docs/NATIVE_PILOT_CANARY.md, GREX local scratch and disk telemetry).
"""
import glob
import json
import os
import re
import subprocess
import time

ROOT = '/var/lib/condor-ce/spool'
JOB = re.compile(r'PanDA_Pilot-([0-9]+)$')
PILOT = re.compile(r'pilot-[0-9]+$')
INTAKE = re.compile(rb'intake_deadline=([0-9]+)')
STARTED = re.compile(rb'^([0-9T:Z-]+) epicprod-multipilot: launcher=')
DEADLINE = time.monotonic() + 420


def emit(**record):
    print(json.dumps(record, sort_keys=True), flush=True)


def space():
    s = os.statvfs(ROOT)
    return dict(free_bytes=s.f_bavail * s.f_frsize, total_bytes=s.f_blocks * s.f_frsize,
                free_inodes=s.f_favail, total_inodes=s.f_files)


def du(path, depth):
    """Allocated KiB per entry to the given depth, files included."""
    result = subprocess.run(['du', '-akx', '--max-depth=%d' % depth, '--', path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=max(5, DEADLINE - time.monotonic()))
    sizes = {}
    for line in result.stdout.splitlines():
        kib, _, name = line.partition('\t')
        if kib.isdigit():
            sizes[name] = int(kib) * 1024
    return sizes, result.returncode, result.stderr[-300:]


def head(path, limit=8192):
    try:
        with open(path, 'rb') as handle:
            return handle.read(limit)
    except OSError:
        return b''


def main():
    uid, now = os.getuid(), time.time()
    emit(phase='start', at=now, host=os.uname()[1], uid=uid, **space())
    sandboxes = []
    for name in glob.glob(ROOT + '/*/*/cluster*.proc*.subproc*'):
        try:
            info = os.lstat(name)
        except OSError:
            continue
        if info.st_uid == uid and not os.path.islink(name) and os.path.isdir(name):
            sandboxes.append(name)
    totals = dict(sandboxes=0, old_runtime=0, other=0, bytes_old=0, bytes_other=0,
                  job_dirs=0, job_bytes=0, logtgz_bytes=0, alive_pilots=0, time_limited=False)
    for sandbox in sorted(sandboxes):
        if time.monotonic() > DEADLINE:
            totals['time_limited'] = True
            break
        cluster = re.search(r'cluster([0-9]+)\.proc([0-9]+)', sandbox)
        try:
            sizes, rc, err = du(sandbox, 3)
        except subprocess.TimeoutExpired:
            totals['time_limited'] = True
            break
        record = dict(sandbox=sandbox, ce_job='%s.%s' % cluster.groups(),
                      bytes=sizes.get(sandbox, 0), du_rc=rc)
        if rc:
            record['du_error'] = err
        stdout = b''.join(head(p) for p in glob.glob(sandbox + '/_condor_stdout') +
                          glob.glob(sandbox + '/*/_condor_stdout'))
        found = INTAKE.search(stdout)
        if found:
            record['intake_deadline'] = int(found.group(1))
        for line in stdout.splitlines():
            found = STARTED.match(line)
            if found:
                record['launcher_started'] = found.group(1).decode()
                break
        record['relocated'] = b'epicprod-multipilot: worker directory' in stdout
        pilots = [p for p in sizes if PILOT.search(p) and os.path.isdir(p)]
        if pilots:
            jobs, logs, alive = [], 0, 0
            for pilot in pilots:
                try:
                    if now - os.stat(pilot + '/pilotlog.txt').st_mtime < 600:
                        alive += 1
                except OSError:
                    pass
            for path, size in sizes.items():
                parent = os.path.dirname(path)
                if not PILOT.search(parent):
                    continue
                match = JOB.search(path)
                if match:
                    try:
                        mtime = os.stat(path).st_mtime
                    except OSError:
                        mtime = None
                    jobs.append([int(match.group(1)), size, mtime])
                elif path.endswith('.log.tgz'):
                    logs += size
            record.update(runtime='spool-pilots', pilots=len(pilots), alive_pilots=alive,
                          job_dirs=sorted(jobs), logtgz_bytes=logs)
            totals['old_runtime'] += 1
            totals['bytes_old'] += record['bytes']
            totals['job_dirs'] += len(jobs)
            totals['job_bytes'] += sum(size for _, size, _ in jobs)
            totals['logtgz_bytes'] += logs
            totals['alive_pilots'] += alive
        else:
            record['runtime'] = 'no-spool-pilots'
            totals['other'] += 1
            totals['bytes_other'] += record['bytes']
        totals['sandboxes'] += 1
        emit(**record)
    emit(phase='complete', at=time.time(), totals=totals, **space())
    # A grid job that finishes before pandaharvester01's gridmanager sees it
    # running reads there as idle after a start, which the schedd's
    # SYSTEM_PERIODIC_REMOVE removes before the output returns.
    time.sleep(150)


if __name__ == '__main__':
    main()
