"""Backfill the storage record's arrival counters (STORAGE.md, Backfill).

Reconstructs the storage component's cumulative arrival counters per RSE
(arrived, first copies, replicas; files and bytes) and per target
campaign (arrived, archived) at a daily grid of instants before the
census, from the pass's store: every file's DID creation time, its
available replicas, and each dataset replica's creation time per RSE.
A file's first copy goes to its sole RSE or to the RSE whose dataset
replica was created first, the pass's own rule at the census, at the
file's creation time; each further replica goes to its RSE at the later
of the file's creation and that dataset replica's creation; the archive
is the first tape replica. One synthetic snap per grid instant is
written with capture policy ``backfill-storage-v1``, reconstructed
evidence distinguishable from observed snaps, carrying only the flow
counters in the publisher's envelope shape, on the census's absolute
origin: every counter is the count of events at or before the instant,
so the backfilled grid and the live counter chain form one record.
Gauges (inventory, ghosts, backlog, capacity) are not reconstructible
and begin at the census.

Idempotent: --apply first removes prior backfill-storage-v1 snaps for
the scope and writes only instants strictly before the earliest live
storage snap. Dry-run default, printing the seam: the reconstructed
totals at the last instant against the census counters.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/backfill_storage_arrivals.py \\
        [--days 30] [--store /data/wenauseic/swf-delivery/storage.sqlite] \\
        [--apply]
"""

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from monitor_app.snapper_storage import (  # noqa: E402
    ASSESSMENT_POLICY_VERSION,
    COMPONENT_NAME,
    PUBLISHER_IDENTITY,
    SCOPE,
    STORAGE_REGISTRATION,
)
from snapper_ai.models import SystemSnap  # noqa: E402

CAPTURE_POLICY = 'backfill-storage-v1'
DEFAULT_STORE = '/data/wenauseic/swf-delivery/storage.sqlite'
ET = ZoneInfo('America/New_York')
RSE_KEYS = ('arrived_files', 'arrived_bytes', 'first_copy_files',
            'first_copy_bytes', 'replica_files', 'replica_bytes')
CAMPAIGN_KEYS = ('arrived_files', 'arrived_bytes', 'archived_files',
                 'archived_bytes')


def _parse(stamp):
    """An ISO stamp from the store or the catalog as an aware UTC time;
    None when absent or unreadable."""
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _live_first():
    """The earliest observed snap carrying the storage component: the
    seam, and the census's vocabulary (RSEs, their types, the target
    campaigns) and counters."""
    snap = (SystemSnap.objects
            .filter(scope=SCOPE, state__components__has_key=COMPONENT_NAME)
            .exclude(capture_policy=CAPTURE_POLICY)
            .order_by('snap_time').first())
    if snap is None:
        return None, {}
    payload = (snap.state or {}).get('components', {}).get(COMPONENT_NAME) or {}
    return snap, payload.get('data') or {}


def _grid(seam, days):
    """Daily instants at Eastern midnight, from ``days`` days before the
    seam's day through the last midnight strictly before the seam."""
    last = seam.astimezone(ET).replace(hour=0, minute=0, second=0,
                                       microsecond=0)
    if last >= seam:
        last -= dt.timedelta(days=1)
    return [last - dt.timedelta(days=n) for n in range(days, -1, -1)]


def _dataset_replica_times(store):
    """{dataset path: {rse: created}} from the dataset replica summaries
    the census stored, keyed as the files' location column is keyed
    (no leading slash)."""
    times = {}
    for name, summary in store.execute(
            'SELECT name, summary FROM datasets WHERE summary IS NOT NULL'):
        try:
            rows = json.loads(summary)
        except (TypeError, ValueError):
            continue
        per_rse = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get('rse'):
                continue
            created = _parse(row.get('created_at'))
            if created is not None:
                per_rse[str(row['rse'])] = created
        if per_rse:
            times[str(name).lstrip('/')] = per_rse
    return times


def _day(when):
    return when.astimezone(ET).date()


def reconstruct(store_path, targets, tape_rses):
    """Per-day event buckets: {day: {(kind, key, counter): count}} for
    the RSE and campaign counters, plus the file and dataset counts.
    Streams the store once; memory is the number of days times the
    vocabulary, never the file count."""
    store = sqlite3.connect(f'file:{store_path}?mode=ro', uri=True)
    ds_times = _dataset_replica_times(store)
    buckets = defaultdict(lambda: defaultdict(int))
    rses_seen = set()
    files = 0
    query = ('SELECT f.location, f.campaign, f.bytes, f.created_at, '
             'GROUP_CONCAT(r.rse) FROM files f JOIN replicas r '
             "ON r.name = f.name AND r.state = 'AVAILABLE' "
             'WHERE f.gone_at IS NULL GROUP BY f.name')
    for location, campaign, size, created_at, rse_list in store.execute(query):
        created = _parse(created_at)
        if created is None or not rse_list:
            continue
        files += 1
        size = int(size or 0)
        available = sorted(set(rse_list.split(',')))
        rses_seen.update(available)
        per_rse = ds_times.get(str(location or '').lstrip('/'), {})
        # The pass's rule (storage.py, _first_rse_by_location and
        # first_sight): the sole RSE; else the RSE whose dataset replica
        # was created first, over every RSE of the dataset, when the
        # file is available there; else the alphabetically first.
        if len(available) == 1:
            first = available[0]
        else:
            earliest = (min(per_rse.items(), key=lambda kv: kv[1])[0]
                        if per_rse else None)
            first = earliest if earliest in available else available[0]
        day = _day(created)
        for rse in available:
            if rse == first:
                arrival = created
                kind = 'first_copy'
            else:
                arrival = max(created, per_rse.get(rse) or created)
                kind = 'replica'
            bucket = buckets[_day(arrival)]
            bucket[('rse', rse, 'arrived_files')] += 1
            bucket[('rse', rse, 'arrived_bytes')] += size
            bucket[('rse', rse, f'{kind}_files')] += 1
            bucket[('rse', rse, f'{kind}_bytes')] += size
        if campaign in targets:
            bucket = buckets[day]
            bucket[('campaign', campaign, 'arrived_files')] += 1
            bucket[('campaign', campaign, 'arrived_bytes')] += size
            tape_arrivals = [max(created, per_rse.get(r) or created)
                             for r in available if r in tape_rses]
            if tape_arrivals:
                bucket = buckets[_day(min(tape_arrivals))]
                bucket[('campaign', campaign, 'archived_files')] += 1
                bucket[('campaign', campaign, 'archived_bytes')] += size
    datasets = store.execute('SELECT COUNT(*) FROM datasets').fetchone()[0]
    store.close()
    return buckets, sorted(rses_seen), files, int(datasets or 0)


def counters_at(instants, buckets):
    """Cumulative counters at each instant: every event on a day that
    ended at or before the instant (instants are Eastern midnights, so
    a day belongs to the instants after it)."""
    days = sorted(buckets)
    totals = defaultdict(int)
    results = []
    index = 0
    for instant in instants:
        instant_day = instant.astimezone(ET).date()
        while index < len(days) and days[index] < instant_day:
            for key, count in buckets[days[index]].items():
                totals[key] += count
            index += 1
        results.append((instant, dict(totals)))
    return results


def _data(instant, previous, totals, rses, types, targets, files, datasets):
    def block(kind, name, keys):
        return {key: int(totals.get((kind, name, key)) or 0) for key in keys}
    return {
        'interval': {'start': previous.isoformat() if previous else None,
                     'end': instant.isoformat()},
        'pass': {'mode': 'backfill', 'campaigns': list(targets),
                 'files_checked': files, 'datasets_checked': datasets,
                 'duration_s': 0, 'error_count': 0, 'errors': []},
        'rses': {rse: {'type': types.get(rse, ''),
                       'flow': block('rse', rse, RSE_KEYS)}
                 for rse in rses},
        'campaigns': {name: {'flow': block('campaign', name, CAMPAIGN_KEYS)}
                      for name in targets},
    }


def main():
    parser = argparse.ArgumentParser(
        description='Backfill the storage record\'s arrival counters '
                    'into epicprod snap history.')
    parser.add_argument('--days', type=int, default=30,
                        help='days before the census to reconstruct '
                             '(default 30)')
    parser.add_argument('--store', default=DEFAULT_STORE,
                        help='the storage pass\'s store (read-only)')
    parser.add_argument('--apply', action='store_true',
                        help='write the snaps (dry run without)')
    args = parser.parse_args()

    live, census = _live_first()
    if live is None:
        print('no live storage snap: the census has not been published; '
              'nothing to seam against')
        return 1
    seam = live.snap_time
    types = {rse: str((block or {}).get('type') or '')
             for rse, block in (census.get('rses') or {}).items()
             if isinstance(block, dict)}
    tape_rses = {rse for rse, kind in types.items() if kind.upper() == 'TAPE'}
    targets = tuple(sorted(census.get('campaigns') or {}))
    instants = _grid(seam, args.days)
    print(f'seam: first live storage snap {seam.isoformat()}; '
          f'grid {len(instants)} daily instants '
          f'{instants[0].isoformat()} -> {instants[-1].isoformat()}')
    print(f'targets {targets}; tape {sorted(tape_rses)}')

    buckets, rses, files, datasets = reconstruct(args.store, targets, tape_rses)
    results = counters_at(instants, buckets)
    print(f'{files} files with an available replica, {datasets} datasets, '
          f'{len(buckets)} event days, RSEs {rses}')

    _, last = results[-1]
    print('\nseam check, reconstructed at the last instant vs the census counters:')
    for rse in rses:
        live_flow = ((census.get('rses') or {}).get(rse) or {}).get('flow') or {}
        print(f'  {rse:14s} arrived {last.get(("rse", rse, "arrived_files"), 0):>9,} '
              f'vs {int(live_flow.get("arrived_files") or 0):>9,}   '
              f'first {last.get(("rse", rse, "first_copy_files"), 0):>9,} '
              f'vs {int(live_flow.get("first_copy_files") or 0):>9,}   '
              f'replica {last.get(("rse", rse, "replica_files"), 0):>9,} '
              f'vs {int(live_flow.get("replica_files") or 0):>9,}')
    for name in targets:
        live_flow = ((census.get('campaigns') or {}).get(name) or {}).get('flow') or {}
        print(f'  campaign {name:6s} arrived '
              f'{last.get(("campaign", name, "arrived_files"), 0):>9,} vs '
              f'{int(live_flow.get("arrived_files") or 0):>9,}   archived '
              f'{last.get(("campaign", name, "archived_files"), 0):>9,} vs '
              f'{int(live_flow.get("archived_files") or 0):>9,}')
    print('\ndaily first copies at EIC-XRD, last 7 instants:')
    previous = None
    for instant, totals in results[-7:]:
        value = totals.get(('rse', 'EIC-XRD', 'first_copy_files'), 0)
        print(f'  {instant.astimezone(ET).date()} cumulative {value:,}'
              + (f' (+{value - previous:,})' if previous is not None else ''))
        previous = value

    if not args.apply:
        print('\ndry run: nothing written; --apply writes the snaps')
        return 0

    now = timezone.now()
    removed = SystemSnap.objects.filter(
        scope=SCOPE, capture_policy=CAPTURE_POLICY).delete()
    written = 0
    previous = None
    registration_version = int(
        ((live.state or {}).get('components', {}).get(COMPONENT_NAME) or {})
        .get('registration_version') or 1)
    for instant, totals in results:
        if instant >= seam:
            break
        data = _data(instant, previous, totals, rses, types, targets,
                     files, datasets)
        # One second past the grid instant: live captures land on
        # aligned boundaries, so the stamp never collides with a real
        # snap under the (scope, snap_time) uniqueness.
        SystemSnap.objects.create(
            scope=SCOPE,
            snap_time=instant + dt.timedelta(seconds=1),
            observed_at=now,
            completed_at=now,
            snap_schema_version=1,
            capture_policy=CAPTURE_POLICY,
            encoding='full',
            reasons=['backfill'],
            changed_components=[COMPONENT_NAME],
            component_revisions={COMPONENT_NAME: 0},
            registration_versions={COMPONENT_NAME: registration_version},
            component_hashes={},
            state_hash='',
            state={'components': {COMPONENT_NAME: {
                'v': 1,
                'data': data,
                'registration': STORAGE_REGISTRATION,
                'revision': 0,
                'registration_version': registration_version,
                'assessed_at': instant.isoformat(),
                'source_as_of': instant.isoformat(),
                'accepted_at': now.isoformat(),
                'assessment_policy': ASSESSMENT_POLICY_VERSION,
                'publisher_identity': PUBLISHER_IDENTITY,
            }}},
        )
        previous = instant
        written += 1
    print(f'\napplied: removed prior backfill {removed[0]}, '
          f'wrote {written} snaps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
