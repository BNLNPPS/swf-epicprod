#!/usr/bin/env python3
"""The actual mismatches on the OSG pools: which of our requirements turns away
slots that are free and open, pool by pool, in slots and cores.

Every slot ad is read once with the attributes our requirements use. A slot is
first classed by the pool's own gates, which no requirement of ours changes:
claimed, draining, unvalidated, black hole, overloaded, past its glidein's
retirement, or with too little life left for a non-Long job (the glidein START
clause: Max_Walltime defined and >= 43200 and ToDie - now < 36000). The free and
open remainder is then tested against our production pilot's requirements
(ATLAS CVMFS, host RHEL 9, user namespaces, memory, disk, node exclusions,
undesired sites) and the direct native pilot's (Singularity, the image
repository, memory, disk, exclusions, undesired sites), counting for each
clause the slots that fail it and the slots that fail it alone. Python 3.6.

Usage: osg-mismatch-census.py [--pool NAME ...] [--memory MB] [--disk KB] [--timeout S]
"""
import argparse
import subprocess
import sys
import time

POOLS = ['jlab-cm.osg.chtc.io', 'cm-1.ospool.osg-htc.org']
ATTRS = ['Name', 'GLIDEIN_Site', 'Machine', 'State', 'Cpus', 'Memory', 'Disk', 'HasSingularity',
         'HAS_CVMFS_atlas_cern_ch', 'HAS_CVMFS_singularity_opensciencegrid_org', 'OSGVO_OS_STRING',
         'HAS_UNPRIVILEGED_USER_NAMESPACES', 'GLIDEIN_ToRetire', 'GLIDEIN_ToDie', 'GLIDEIN_Max_Walltime',
         'SiteWMS_WN_Draining', 'OSG_NODE_VALIDATED', 'IsBlackHole', 'HasExcessiveLoad', 'PartitionableSlot']
UNDESIRED = {'UCSD', 'FNAL', 'MI-HORUS', 'GREX', 'BNL-SDCC', 'Alabama-CHPC'}
# the static node exclusions of the production template, (site, bare host prefix)
EXCLUDED_NODES = {('Nebraska', 'red-c0823'), ('Nebraska', 'red-c7228'), ('Nebraska', 'red-c0831'),
                  ('UConn', 'nod58'), ('Rhodes-HPC', 'compute05'), ('GREX', 'n358'),
                  ('ComputeCanada-Fir', 'fc30438'), ('ComputeCanada-Fir', 'fc20637'),
                  ('ComputeCanada-Fir', 'fc20621'), ('ComputeCanada-Fir', 'fc20667'),
                  ('ComputeCanada-Fir', 'fc20624'), ('ComputeCanada-Fir', 'fc30416'),
                  ('BEOCAT-SLATE', 'warlock12')}


def read(pool, timeout):
    out = subprocess.run(['condor_status', '-pool', pool, '-af:t'] + ATTRS, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == len(ATTRS):
            rows.append(dict(zip(ATTRS, parts)))
    return rows


def num(value):
    try:
        return float(value)
    except ValueError:
        return None


def pool_gate(slot, now):
    """The pool's own reason a slot takes nothing now, or ''."""
    if slot['State'] != 'Unclaimed':
        return 'claimed or busy'
    if slot['SiteWMS_WN_Draining'] == 'true':
        return 'draining'
    if slot['OSG_NODE_VALIDATED'] == 'false':
        return 'not validated'
    if slot['IsBlackHole'] == 'true' or slot['HasExcessiveLoad'] == 'true':
        return 'black hole or overloaded'
    if (num(slot['Cpus']) or 0) < 1:
        return 'no cores left (partitionable remnant)'
    retire = num(slot['GLIDEIN_ToRetire'])
    if retire is not None and retire <= now:
        return 'glidein retired'
    walltime, die = num(slot['GLIDEIN_Max_Walltime']), num(slot['GLIDEIN_ToDie'])
    if walltime is not None and walltime >= 43200 and die is not None and die - now < 36000:
        return 'under 10 h of glidein life'
    return ''


def our_failures(slot, direct, memory, disk):
    """The names of our requirement clauses this slot fails."""
    failed = []
    mem, dsk = num(slot['Memory']) or 0, num(slot['Disk']) or 0
    if mem < memory:
        failed.append('memory %d MB' % memory)
    if dsk < disk:
        failed.append('disk %d GB' % (disk // 1000000))
    if direct:
        if slot['HasSingularity'] != 'true':
            failed.append('Singularity')
        if slot['HAS_CVMFS_singularity_opensciencegrid_org'] != 'true':
            failed.append('image repository')
    else:
        if slot['HAS_CVMFS_atlas_cern_ch'] != 'true':
            failed.append('ATLAS CVMFS')
        if slot['OSGVO_OS_STRING'] != 'RHEL 9':
            failed.append('host RHEL 9')
        if slot['HAS_UNPRIVILEGED_USER_NAMESPACES'] != 'enabled':
            failed.append('user namespaces')
    site, machine = slot['GLIDEIN_Site'], slot['Machine'].split('.', 1)[0]
    if site in UNDESIRED:
        failed.append('undesired site')
    if (site, machine) in EXCLUDED_NODES:
        failed.append('excluded node')
    return failed


def census(rows, direct, memory, disk, now):
    gates, fails, alone = {}, {}, {}
    free_open, willing = [0, 0], [0, 0]
    for slot in rows:
        cores = int(num(slot['Cpus']) or 0)
        gate = pool_gate(slot, now)
        if gate:
            g = gates.setdefault(gate, [0, 0]); g[0] += 1; g[1] += cores
            continue
        free_open[0] += 1; free_open[1] += cores
        failed = our_failures(slot, direct, memory, disk)
        if not failed:
            willing[0] += 1; willing[1] += cores
            continue
        for clause in failed:
            f = fails.setdefault(clause, [0, 0]); f[0] += 1; f[1] += cores
        if len(failed) == 1:
            a = alone.setdefault(failed[0], [0, 0]); a[0] += 1; a[1] += cores
    return gates, free_open, fails, alone, willing


def report(pool, rows, memory, disk, now):
    print('== %s: %d slot ads, %d cores, %s' % (pool, len(rows), sum(int(num(r['Cpus']) or 0) for r in rows),
                                                time.strftime('%H:%M:%SZ', time.gmtime(now))))
    gates, free_open, fails_p, alone_p, willing_p = census(rows, False, memory, disk, now)
    _, _, fails_d, alone_d, willing_d = census(rows, True, memory, disk, now)
    print('   closed by the pool itself, no requirement of ours involved:')
    for gate, (n, c) in sorted(gates.items(), key=lambda kv: -kv[1][0]):
        print('     %6d slots %7d cores  %s' % (n, c, gate))
    print('   free and open: %d slots, %d cores' % tuple(free_open))
    for label, fails, alone, willing in (('production pilot', fails_p, alone_p, willing_p),
                                         ('direct native pilot', fails_d, alone_d, willing_d)):
        print('   %s: willing %d slots, %d cores' % (label, willing[0], willing[1]))
        print('     our requirement       turns away slots/cores   is the only reason for slots/cores')
        for clause, (n, c) in sorted(fails.items(), key=lambda kv: -kv[1][0]):
            a = alone.get(clause, [0, 0])
            print('     %-20s %8d %8d           %8d %8d' % (clause, n, c, a[0], a[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pool', action='append', help='a collector; default both')
    parser.add_argument('--memory', type=int, default=4096, help='our request in MB')
    parser.add_argument('--disk', type=int, default=4500000, help='our request in KB')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    now = time.time()
    status = 0
    for pool in args.pool or POOLS:
        try:
            rows = read(pool, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print('== %s: not read (%s)' % (pool, exc))
            status = 1
            continue
        report(pool, rows, args.memory, args.disk, now)
    return status


if __name__ == '__main__':
    sys.exit(main())
