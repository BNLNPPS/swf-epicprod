"""The names of the job metrics ePIC adds to a PanDA job record
(EPICPROD_PAYLOAD.md, The job digest).

PanDA keeps 500 characters of a job's metrics and cuts the rest
(pilot_api.py), and the pilot's own and JEDI's keys share them, so ours are
short. This one table is read by every writer and every reader: the
payload's digest (payload_report.py, on the worker, as a sibling module),
the native pilot's disk readings (pilot/native-canary/build.py), and the
monitor, the canary and PCS (as swf_epicprod.payload.metric_keys). Jobs
written before the short names carry the long ones, which stay readable:
a reader takes the short name first, then the long one.
"""
import re

# field: (name written now, name written before 2026-10-28 or None)
NAMES = {
    # the payload's digest (payload_report.job_metrics)
    'stage': ('pStage', 'payloadStage'),
    'stage_status': ('pStat', 'payloadStageStatus'),
    'trail': ('pTrail', 'payloadTrail'),
    'registration': ('pReg', 'payloadRegistration'),
    'version': ('pVer', 'payloadVersion'),
    'threads': ('pThr', 'payloadThreads'),
    'exit': ('pExit', 'payloadExit'),
    'fatal_signal': ('pSig', 'payloadFatalSignal'),
    'fatal_stage': ('pSigStage', 'payloadFatalStage'),
    'fatal_stalled': ('pStalled', 'payloadFatalStalled'),
    'log_mb': ('pLogMB', None),
    # the native pilot's disk readings (native_container.disk_headroom,
    # scratch_peak): the computing element's directory (ce), the pilot's
    # (pl, written only where it differs from the scratch's) and the job's
    # scratch (sc), each free and total space and inodes, and the time read
    'disk_sample': ('dT', 'diskSample'),
    'ce_free_mb': ('ceFree', 'ceFreeMB'),
    'ce_total_mb': ('ceTot', 'ceTotalMB'),
    'ce_inodes_free': ('ceIFree', 'ceIFree'),
    'ce_inodes_total': ('ceITot', 'ceITotal'),
    'pilot_free_mb': ('plFree', 'pilotFreeMB'),
    'pilot_total_mb': ('plTot', 'pilotTotalMB'),
    'pilot_inodes_free': ('plIFree', 'pilotIFree'),
    'pilot_inodes_total': ('plITot', 'pilotITotal'),
    'scratch_free_mb': ('scFree', 'scratchFreeMB'),
    'scratch_total_mb': ('scTot', 'scratchTotalMB'),
    'scratch_inodes_free': ('scIFree', 'scratchIFree'),
    'scratch_inodes_total': ('scITot', 'scratchITotal'),
    'scratch_peak_mb': ('scPeakMB', 'scratchPeakMB'),
    'scratch_peak_files': ('scPeakFiles', 'scratchPeakFiles'),
    # the job's events: the pilot's own count, which the payload's
    # payloadEvents repeated on all but a handful of jobs
    'events': ('nEvents', 'payloadEvents'),
}

# The disk readings by area, as the native pilot labels them, and the four
# readings of each.
DISK_AREAS = {'ce': 'ce', 'pilot': 'pl', 'scratch': 'sc'}
DISK_READINGS = {'FreeMB': 'Free', 'TotalMB': 'Tot', 'IFree': 'IFree', 'ITotal': 'ITot'}


def name(field):
    """The name a writer uses for ``field`` now."""
    return NAMES[field][0]


def names(field):
    """Every name ``field`` has been written under, the current one first."""
    return tuple(n for n in NAMES[field] if n)


_TOKEN = re.compile(r'(?:^|\s)([A-Za-z0-9_]+)=(\S*)')


def parse(text):
    """The fields of a job metrics string, by field, each value as written
    (a string): the current name's value where present, else the old
    name's. Keys outside the table are returned under their own names."""
    raw = dict(_TOKEN.findall(text or ''))
    out = {}
    for field, pair in NAMES.items():
        for key in pair:
            if key and key in raw:
                out[field] = raw[key]
                break
    known = {k for pair in NAMES.values() for k in pair if k}
    out.update({k: v for k, v in raw.items() if k not in known})
    return out


def _alternation(field):
    return '|'.join(names(field))


def sql_value(field, column='jobmetrics', value='[^[:space:]]+'):
    """A PostgreSQL expression for ``field``'s value in a job metrics column
    under any of its names (NULL when absent), for SELECT and GROUP BY.
    ``value`` is the value's own pattern."""
    return (f"substring({column} from '(?:^|[[:space:]])(?:{_alternation(field)})=({value})')")


def regex(field, value):
    """A PostgreSQL regular expression matching ``field`` equal to ``value``
    under any of its names, for ``column ~ %s`` with this as the parameter.
    ``value`` is taken literally."""
    return f"(^|[[:space:]])({_alternation(field)})={re.escape(str(value))}([[:space:]]|$)"
