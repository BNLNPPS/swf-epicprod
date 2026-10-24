#!/usr/bin/env python3
"""The actual mismatches on the OSG pools: which of our requirements turns away
slots that are free and open, pool by pool, in slots and cores.

Every slot ad is read once with the attributes our requirements use. A slot is
first classed by the pool's own gates, which no requirement of ours changes:
claimed, draining, unvalidated, black hole, overloaded, past its glidein's
retirement, or with too little life left for a non-Long job (the glidein START
clause: Max_Walltime defined and >= 43200 and ToDie - now < 36000). The free and
open remainder is then tested against the requirements of the pilot we
submit, read from the live production template on every run: each clause the
template's Requirements carries (of Singularity, the image repository, the CPU
level, ATLAS CVMFS, host RHEL 9, user namespaces), memory, disk, the template's
node exclusions and its undesired sites, counting for each clause the slots
that fail it and the slots that fail it alone. A clause the template drops is
no longer counted. Python 3.6.

With --record FILE one JSON line per pool is appended, the counts above and
nothing else, so a run every ten minutes gives the day's curve of what each
requirement costs; the line names the clauses it modeled.

With --exact the collector also does the matchmaker's two-sided match slot by
slot (the ClassAd bindings, user priority ignored) for the pilot ad built from
the live production template, against
the unclaimed slots that meet the resource requests, and sums the free cores
of the willing ones: the cores one-core pilots could take now. That is the
willing figure, in slots and cores; the modeled counts above are the clause
diagnosis. Our priority row from each negotiator comes with it.

Usage: osg-mismatch-census.py [--pool NAME ...] [--memory MB] [--disk KB] [--timeout S]
                              [--record FILE] [--exact] [--template SDF]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

POOLS = ['jlab-cm.osg.chtc.io', 'cm-1.ospool.osg-htc.org']
ATTRS = ['Name', 'GLIDEIN_Site', 'Machine', 'State', 'Cpus', 'Memory', 'Disk', 'HasSingularity',
         'HAS_CVMFS_atlas_cern_ch', 'HAS_CVMFS_singularity_opensciencegrid_org', 'OSGVO_OS_STRING',
         'HAS_UNPRIVILEGED_USER_NAMESPACES', 'GLIDEIN_ToRetire', 'GLIDEIN_ToDie', 'GLIDEIN_Max_Walltime',
         'SiteWMS_WN_Draining', 'OSG_NODE_VALIDATED', 'IsBlackHole', 'HasExcessiveLoad', 'PartitionableSlot',
         'Microarch']
# The clauses a template's Requirements may carry: (the attribute naming it,
# the clause's name, the slot passes it).
CLAUSES = (
    ('HasSingularity', 'Singularity', lambda s: s['HasSingularity'] == 'true'),
    ('HAS_CVMFS_singularity_opensciencegrid_org', 'image repository',
     lambda s: s['HAS_CVMFS_singularity_opensciencegrid_org'] == 'true'),
    ('Microarch', 'CPU x86_64-v2', lambda s: s['Microarch'] != 'x86_64-v2'),
    ('HAS_CVMFS_atlas_cern_ch', 'ATLAS CVMFS', lambda s: s['HAS_CVMFS_atlas_cern_ch'] == 'true'),
    ('OSGVO_OS_STRING', 'host RHEL 9', lambda s: s['OSGVO_OS_STRING'] == 'RHEL 9'),
    ('HAS_UNPRIVILEGED_USER_NAMESPACES', 'user namespaces',
     lambda s: s['HAS_UNPRIVILEGED_USER_NAMESPACES'] == 'enabled'),
)


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


def template_model(path):
    """What the live template requires: its clauses, its node exclusions
    {(site, host prefix)} and its undesired sites."""
    text = open(path).read()
    requirements = template_value(text, 'Requirements')
    clauses = [(name, test) for attr, name, test in CLAUSES if attr in requirements]
    excluded = set()
    for site, hosts in re.findall(r'GLIDEIN_Site =\?= "([^"]+)" && regexp\("\^\(([^)]*)\)', requirements):
        excluded.update((site, h) for h in hosts.split('|'))
    undesired = {s.strip() for s in template_value(text, '+UNDESIRED_Sites').strip('"').split(',') if s.strip()}
    return {'clauses': clauses, 'excluded': excluded, 'undesired': undesired}


def our_failures(slot, model, memory, disk):
    """The names of our requirement clauses this slot fails."""
    failed = []
    mem, dsk = num(slot['Memory']) or 0, num(slot['Disk']) or 0
    if mem < memory:
        failed.append('memory %d MB' % memory)
    if dsk < disk:
        failed.append('disk %d GB' % (disk // 1000000))
    for name, test in model['clauses']:
        if not test(slot):
            failed.append(name)
    site, machine = slot['GLIDEIN_Site'], slot['Machine'].split('.', 1)[0]
    if site in model['undesired']:
        failed.append('undesired site')
    if (site, machine) in model['excluded']:
        failed.append('excluded node')
    return failed


def census(rows, model, memory, disk, now):
    gates, fails, alone = {}, {}, {}
    free_open, willing = [0, 0], [0, 0]
    for slot in rows:
        cores = int(num(slot['Cpus']) or 0)
        gate = pool_gate(slot, now)
        if gate:
            g = gates.setdefault(gate, [0, 0]); g[0] += 1; g[1] += cores
            continue
        free_open[0] += 1; free_open[1] += cores
        failed = our_failures(slot, model, memory, disk)
        if not failed:
            willing[0] += 1; willing[1] += cores
            continue
        for clause in failed:
            f = fails.setdefault(clause, [0, 0]); f[0] += 1; f[1] += cores
        if len(failed) == 1:
            a = alone.setdefault(failed[0], [0, 0]); a[0] += 1; a[1] += cores
    return gates, free_open, fails, alone, willing


def report(pool, rows, model, memory, disk, now, record=None, exact_counts=None):
    print('== %s: %d slot ads, %d cores, %s' % (pool, len(rows), sum(int(num(r['Cpus']) or 0) for r in rows),
                                                time.strftime('%H:%M:%SZ', time.gmtime(now))))
    gates, free_open, fails_p, alone_p, willing_p = census(rows, model, memory, disk, now)
    modeled = [name for name, _ in model['clauses']]
    if record:
        line = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'pool': pool, 'ads': len(rows),
                'cores': sum(int(num(r['Cpus']) or 0) for r in rows), 'memory_mb': memory, 'disk_kb': disk,
                'gates': gates, 'free_open': free_open,
                'production': {'willing': willing_p, 'fails': fails_p, 'alone': alone_p,
                               'model': 'live template', 'clauses': modeled}}
        if exact_counts is not None:
            line['exact'] = exact_counts
        with open(record, 'a') as out:
            out.write(json.dumps(line, sort_keys=True) + '\n')
    print('   closed by the pool itself, no requirement of ours involved:')
    for gate, (n, c) in sorted(gates.items(), key=lambda kv: -kv[1][0]):
        print('     %6d slots %7d cores  %s' % (n, c, gate))
    print('   free and open: %d slots, %d cores' % tuple(free_open))
    print('   the pilot we submit (live template; clauses %s):' % ', '.join(modeled))
    for label, fails, alone, willing in (('production pilot', fails_p, alone_p, willing_p),):
        print('   %s: modeled willing %d slots, %d cores%s' % (
            label, willing[0], willing[1],
            ('; matched: willing %d slots %d cores, our requirements reject %d, their gates reject %d'
             % (exact_counts[label.split()[0]]['willing'][0], exact_counts[label.split()[0]]['willing'][1],
                exact_counts[label.split()[0]]['rejected_by_ours'][0], exact_counts[label.split()[0]]['reject_us'][0]))
            if exact_counts and label.split()[0] in exact_counts else ''))
        print('     our requirement       turns away slots/cores   is the only reason for slots/cores')
        for clause, (n, c) in sorted(fails.items(), key=lambda kv: -kv[1][0]):
            a = alone.get(clause, [0, 0])
            print('     %-20s %8d %8d           %8d %8d' % (clause, n, c, a[0], a[1]))


TEMPLATE = '/var/data/atlpan/harvester_common/submit_pilot2_pull_bnl_osg.sdf'
AUTO = (' && (TARGET.Arch == "X86_64") && (TARGET.OpSys == "LINUX") && (TARGET.Disk >= RequestDisk)'
        ' && (TARGET.Memory >= RequestMemory) && (TARGET.HasFileTransfer)')


def template_value(text, key):
    found = re.findall(r'(?m)^' + re.escape(key) + r'\s*=\s*(.*)$', text)
    if len(found) != 1:
        raise ValueError('expected one %s in the template' % key)
    return found[0].strip()


def job_ad(template, memory, disk):
    """The pilot ad as ClassAd text, from the live template."""
    text = open(template).read()
    requirements = template_value(text, 'Requirements')
    want = 'WantSingularity = true\n' if 'HasSingularity' in requirements else ''
    return ('MyType = "Job"\nTargetType = "Machine"\nJobUniverse = 5\nOwner = "atlpan"\nClusterId = 1\n'
            'ProcId = 0\nJobStatus = 1\nRequestCpus = 1\nRequestMemory = %d\nRequestDisk = %d\n'
            'JobDurationCategory = "Medium"\nProjectName = "EIC"\nUNDESIRED_Sites = %s\nEpicprodNodeGuard = %s\n'
            '%sRequirements = (%s)%s\n'
            % (memory, disk, template_value(text, '+UNDESIRED_Sites'), template_value(text, '+EpicprodNodeGuard'),
               want, requirements, AUTO))


def exact(pool, memory, disk, template, timeout):
    """The matchmaker's two-sided counts for the pilot we submit, and our priority row."""
    work = tempfile.mkdtemp(prefix='osg-census-')
    try:
        slots = os.path.join(work, 'slots')
        with open(slots, 'w') as out:
            fetched = subprocess.run(['condor_status', '-pool', pool, '-constraint',
                                      'State=="Unclaimed" && Cpus>=1 && Memory>=%d && Disk>=%d' % (memory, disk),
                                      '-long'], stdout=out, stderr=subprocess.PIPE, universal_newlines=True,
                                     timeout=timeout)
        if fetched.returncode != 0:
            raise RuntimeError(fetched.stderr.strip())
        import classad2 as classad  # the system bindings on osgsub01
        ads = list(classad.parseAds(open(slots).read()))
        result = {'reduced_ads': len(ads)}
        for label, text in (('production', job_ad(template, memory, disk)),):
            job = classad.parseOne(text)
            counts = {'willing': [0, 0], 'rejected_by_ours': [0, 0], 'reject_us': [0, 0]}
            for slot in ads:
                cores = int(slot.get('Cpus', 0) or 0)
                we_accept = slot.matches(job)      # our Requirements, the slot as target
                accepts_us = job.matches(slot)     # the slot's START, our pilot as target
                if we_accept and accepts_us:
                    key = 'willing'
                    counts[key][0] += 1; counts[key][1] += cores
                    continue
                if not we_accept:
                    counts['rejected_by_ours'][0] += 1; counts['rejected_by_ours'][1] += cores
                if not accepts_us:
                    counts['reject_us'][0] += 1; counts['reject_us'][1] += cores
            result[label] = counts
        prio = subprocess.run(['condor_userprio', '-pool', pool, '-all', '-allusers'], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
        row = [line.split() for line in prio.stdout.splitlines() if line.startswith('atlpan@osgsub01')]
        if row:
            result['priority'] = {'effective': float(row[0][1]), 'real': float(row[0][2]),
                                  'factor': float(row[0][3]), 'in_use': int(row[0][4])}
        return result
    finally:
        for name in os.listdir(work):
            os.unlink(os.path.join(work, name))
        os.rmdir(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pool', action='append', help='a collector; default both')
    parser.add_argument('--memory', type=int, default=4096, help='our request in MB')
    parser.add_argument('--disk', type=int, default=4500000, help='our request in KB')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--record', help='append one JSON line per pool to this file')
    parser.add_argument('--exact', action='store_true', help="add the matchmaker's two-sided counts and our priority")
    parser.add_argument('--template', default=TEMPLATE, help='the production pull template the pilot ads are built from')
    args = parser.parse_args()
    now = time.time()
    status = 0
    model = template_model(args.template)
    for pool in args.pool or POOLS:
        try:
            rows = read(pool, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print('== %s: not read (%s)' % (pool, exc))
            status = 1
            continue
        exact_counts = None
        if args.exact:
            try:
                exact_counts = exact(pool, args.memory, args.disk, args.template, args.timeout)
            except Exception as exc:  # noqa: BLE001
                print('   exact analysis not done (%s)' % exc)
                status = 1
        report(pool, rows, model, args.memory, args.disk, now, args.record, exact_counts)
    return status


if __name__ == '__main__':
    sys.exit(main())
