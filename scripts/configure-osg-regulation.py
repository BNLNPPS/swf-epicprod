#!/usr/bin/env python3
"""Install the accepted OSG pilot policy; see docs/REGULATION_AUDIT_2026-10-22.md.

Run with Harvester's Python as root on osgsub01. All input files and Python
patch contexts are validated before any write. --apply keeps dated backups;
without it, print the planned paths only. No worker or job is removed.
"""
import argparse
import ast
import configparser
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

QUEUES = ('BNL_OSG_PanDA_1', 'BNL_OSG_EPIC_PROD_1')

# Small compatibility patches to the installed Harvester. The reference
# checkout stays untouched. Exact contexts make an incompatible upgrade fail.
PATCHES = {
    'harvesterbody/worker_adjuster.py': (
        '            # get panda queues dict from CRIC\n',
        '            # ePIC: absent or stale demand must not launch empty pilots.\n'
        '            demand_max_age = getattr(harvester_config.submitter, "jobStatsMaxAge", 0)\n'
        '            if demand_max_age > 0:\n'
        '                demand_at = self.dbProxy.get_cache_last_update_time("job_statistics.json", None)\n'
        '                if demand_at is None or (core_utils.naive_utcnow() - demand_at).total_seconds() > demand_max_age:\n'
        '                    tmp_log.error("REGULATION_HOLD: job demand missing or stale")\n'
        '                    job_stats = {}\n'
        '                    job_stats_new_df = self._job_stats_to_df(None)\n'
        '            # get panda queues dict from CRIC\n'),
    'harvesterbody/cacher.py': (
        '                lastUpdateTime = self.dbProxy.get_cache_last_update_time(mainKey, subKey)\n'
        '                if (not force_update) and lastUpdateTime is not None and lastUpdateTime > timeLimit:',
        '                lastUpdateTime = self.dbProxy.get_cache_last_update_time(mainKey, subKey)\n'
        '                # ePIC: hot demand statistics have their own freshness budget.\n'
        '                refresh_seconds = getattr(harvester_config.cacher, "refreshSecondsByKey", {}).get(mainKey)\n'
        '                itemTimeLimit = (core_utils.naive_utcnow() - datetime.timedelta(seconds=refresh_seconds)\n'
        '                                 if refresh_seconds is not None else timeLimit)\n'
        '                if (not force_update) and lastUpdateTime is not None and lastUpdateTime > itemTimeLimit:'),
    'harvesterbody/monitor.py': (
        '        try:\n            forceEnqueueInterval = harvester_config.monitor.fifoForceEnqueueInterval',
        '        # ePIC: an operational cadence ceiling also applies to workers\n'
        '        # carrying an older immutable queue configuration.\n'
        '        policyInterval = getattr(harvester_config.monitor, "fifoMaxCheckInterval", 0)\n'
        '        if policyInterval > 0:\n'
        '            fifoCheckInterval = min(fifoCheckInterval, policyInterval)\n'
        '        try:\n            forceEnqueueInterval = harvester_config.monitor.fifoForceEnqueueInterval'),
    'harvestermonitor/htcondor_monitor.py': (
        '        self.useCondorHistoryMaxAge = getattr(self, "useCondorHistoryMaxAge", 7200)',
        '        self.useCondorHistoryMaxAge = max(getattr(self, "useCondorHistoryMaxAge", 7200),\n'
        '                                        getattr(harvester_config.monitor, "historyMaxAgeFloor", 0))'),
}


def set_ini(text, section, key, value):
    pattern = re.compile(r'(?ms)^\[' + re.escape(section) + r'\][^\n]*\n(.*?)(?=^\[|\Z)')
    match = pattern.search(text)
    if not match:
        raise ValueError(f'missing [{section}]')
    body = match.group(1)
    assignment = re.compile(r'(?m)^' + re.escape(key) + r'[ \t]*=[^\n]*(?:\n[ \t]+[^\n]*)*')
    if len(assignment.findall(body)) > 1:
        raise ValueError(f'duplicate [{section}] {key}')
    line = f'{key} = {value}'
    body = assignment.sub(line, body) if assignment.search(body) else body.rstrip() + '\n' + line + '\n\n'
    return text[:match.start(1)] + body + text[match.end(1):]


def prepare(root):
    cfg = root / 'etc/panda/panda_harvester.cfg'
    queues = root / 'etc/panda/panda_queueconfig.json'
    text = cfg.read_text()
    settings = {
        'submitter': {'lookupTime': 20, 'jobStatsMaxAge': 90},
        'monitor': {'fifoCheckInterval': 30, 'fifoMaxCheckInterval': 30,
                    'pluginCacheRefreshInterval': 30, 'historyMaxAgeFloor': 86400},
        'cacher': {'sleepTime': 5,
                   'refreshSecondsByKey': '\n ' + json.dumps({'job_statistics.json': 30,
                                                      'job_statistics_new.json': 30})},
    }
    for section, values in settings.items():
        for key, value in values.items():
            text = set_ini(text, section, key, value)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    data = json.loads(queues.read_text())
    for queue in QUEUES:
        entry = data[queue]
        if entry['templateQueueName'] != 'production.pull':
            raise ValueError(f'{queue} is not using the audited pull template')
        entry.update(nQueueLimitWorker=600, nQueueLimitWorkerRatio=0,
                     nQueueLimitWorkerMin=600, nQueueLimitWorkerMax=600,
                     maxNewWorkersPerCycle=300)
        entry.setdefault('monitor', {}).update(fifoCheckInterval=30, useCondorHistoryMaxAge=86400)
        entry.setdefault('submitter', {})['noPilotsWhenNoActiveJobs'] = True
    changes = {cfg: text, queues: json.dumps(data, indent=2) + '\n'}
    for queue in QUEUES:
        path = Path(data[queue]['submitter']['templateFile'])
        source = path.read_text()
        if 'request_memory = 3700' not in source and 'request_memory = 4096' not in source:
            raise ValueError(f'unexpected memory request in {path}')
        changes[path] = source.replace('request_memory = 3700', 'request_memory = 4096')
    packages = list(root.glob('lib/python*/site-packages/pandaharvester'))
    if len(packages) != 1:
        raise ValueError(f'expected one installed Harvester package, found {packages}')
    for relative, (old, new) in PATCHES.items():
        path = packages[0] / relative
        source = path.read_text()
        if new not in source:
            if source.count(old) != 1:
                raise ValueError(f'unsupported Harvester source: {path}')
            source = source.replace(old, new)
        ast.parse(source, filename=str(path))
        changes[path] = source
    return {p: s for p, s in changes.items() if p.read_text() != s}


def install(changes):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for path, content in changes.items():
        backup = path.with_name(path.name + '.before-regulation-' + stamp)
        shutil.copy2(path, backup)
        stat = path.stat()
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
                os.fchmod(out.fileno(), stat.st_mode & 0o7777)
                os.fchown(out.fileno(), stat.st_uid, stat.st_gid)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(f'installed {path}; backup {backup}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/harvester'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    planned = prepare(args.root)
    if args.apply:
        install(planned)
    else:
        print('\n'.join(map(str, planned)) or 'already installed')
