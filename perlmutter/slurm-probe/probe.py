#!/usr/bin/env python3
"""Read a Perlmutter worker's Slurm allocation from inside a PanDA job.

Run as the payload of a one-job diagnostic task. Records the Slurm
environment of the job step, the node's memory, the cgroup limits
(memory and CPUs) at every level from the task up to the job, and
the Slurm configuration if the container can see it. The record goes
to jobReport.json, which the pilot ships to PanDA as job metadata, and
to stdout between SLURM-PROBE-BEGIN and SLURM-PROBE-END markers.

The question it answers: whether a step of --cpus-per-task=256 -n 1
fits the memory Slurm grants the job, given the step's default
memory per CPU (SLURM_MEM_PER_CPU).
"""
import glob
import json
import os
import re
import shutil


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        return f"unreadable: {e.__class__.__name__}: {e}"


def cgroup_levels():
    """The limits at each cgroup level from this process up to the root."""
    levels = []
    for line in read('/proc/self/cgroup').splitlines():
        parts = line.split(':', 2)
        if len(parts) != 3:
            continue
        path = parts[2]
        roots = ['/sys/fs/cgroup'] if parts[0] == '0' else \
            [f'/sys/fs/cgroup/{c}' for c in parts[1].split(',') if c]
        for root in roots:
            p = path
            while True:
                d = os.path.join(root, p.lstrip('/'))
                if os.path.isdir(d):
                    entry = {'dir': d}
                    for name in ('memory.max', 'memory.high', 'memory.current',
                                 'memory.limit_in_bytes', 'cpuset.cpus.effective',
                                 'cpuset.cpus', 'cpu.max'):
                        f = os.path.join(d, name)
                        if os.path.exists(f):
                            entry[name] = read(f)
                    levels.append(entry)
                if p in ('/', ''):
                    break
                p = os.path.dirname(p)
    return levels


def slurm_conf():
    """Memory-relevant lines of slurm.conf, from any path the container sees."""
    found = {}
    for path in ['/etc/slurm/slurm.conf', '/var/spool/slurmd/conf-cache/slurm.conf',
                 '/var/spool/slurm/conf-cache/slurm.conf'] + glob.glob('/etc/slurm/*.conf'):
        if not os.path.isfile(path):
            continue
        text = read(path)
        keep = [l for l in text.splitlines()
                if re.search(r'DefMemPer|MaxMemPer|RealMemory|MemSpec|CoreSpec|CpuSpec|'
                             r'SelectType|TaskPlugin|Threads|CPUs=|OverSubscribe|'
                             r'JobAcctGatherParams|ProctrackType', l)
                and not l.lstrip().startswith('#')]
        found[path] = keep[:200]
    return found


def main():
    node = os.environ.get('SLURMD_NODENAME') or os.uname().nodename
    meminfo = {}
    for line in read('/proc/meminfo').splitlines():
        k, _, v = line.partition(':')
        if k in ('MemTotal', 'MemAvailable'):
            meminfo[k] = v.strip()
    record = {
        'probe': 'slurm-probe/1',
        'node': node,
        'slurm_env': {k: v for k, v in sorted(os.environ.items()) if k.startswith('SLURM')},
        'meminfo': meminfo,
        'nproc_affinity': len(os.sched_getaffinity(0)),
        'cpu_count': os.cpu_count(),
        'proc_self_cgroup': read('/proc/self/cgroup'),
        'cgroup_levels': cgroup_levels(),
        'slurm_conf': slurm_conf(),
        'slurm_commands': {c: shutil.which(c) for c in ('srun', 'scontrol', 'sbatch')},
    }
    text = json.dumps(record, indent=1)
    print('SLURM-PROBE-BEGIN')
    print(text)
    print('SLURM-PROBE-END', flush=True)
    with open('jobReport.json', 'w') as f:
        json.dump({'slurm_probe': record}, f)


if __name__ == '__main__':
    main()
