"""Backfill the campaign delivered-data history from JLab Rucio file
registration times (CAMPAIGN_DELIVERY.md, Backfill).

Reconstructs the daily delivery record for the target campaigns: the
complete file inventory per root from a ``created_after`` DID search,
``created_at`` and bytes per file from bulk metadata, files bucketed by
dataset location and registration day, locations mapped to physics
configurations through the PCS task output records. One synthetic snap
per day is written at day end with capture policy ``backfill-v1`` —
reconstructed evidence, explicitly distinguishable from observed
snaps — carrying only the delivery component in the live publisher's
envelope shape. Events are null throughout (no recorded events/file;
extension 2); expected-events denominators are the current recorded
chain, marked ``denominators_as_of`` in the payload.

Idempotent: --apply first removes prior backfill-v1 snaps for the
scope, and writes only days that end before the earliest live delivery
snap. Unmapped locations are counted and listed, never dropped
silently. Dry-run default.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/backfill_delivery_history.py \\
        [--campaigns 26.06,26.07] [--apply]
"""

import argparse
import datetime as dt
import json
import os
import ssl
import sys
import urllib.request

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from monitor_app.snapper_delivery import (  # noqa: E402
    ASSESSMENT_POLICY_VERSION,
    DELIVERY_REGISTRATION,
    PUBLISHER_IDENTITY,
)
from pcs.models import Dataset, ProdTask  # noqa: E402
from pcs.services import (JLAB_RUCIO_URL, _jlab_rucio_auth,  # noqa: E402
                          _jlab_rucio_get, _ndjson, campaign_family,
                          pc_request_projection)
from snapper_ai.models import SystemSnap  # noqa: E402

ROOTS = ('/RECO', '/SIMU')
SEARCH_EPOCH = '2026-01-01T00:00:00'
BULK_CHUNK = 500
CAPTURE_POLICY = 'backfill-v1'


def _bulkmeta(token, names, scope='epic'):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps({'dids': [{'scope': scope, 'name': n}
                                for n in names]}).encode()
    req = urllib.request.Request(JLAB_RUCIO_URL + '/dids/bulkmeta',
                                 data=body, method='POST')
    req.add_header('X-Rucio-Auth-Token', token)
    req.add_header('Content-Type', 'application/json')
    text = urllib.request.urlopen(req, context=ctx, timeout=120).read()
    return [json.loads(line) for line in text.decode().splitlines()
            if line.strip()]


def collect_files(campaigns):
    """{(location, day): {'files': n, 'bytes': b}} per campaign, from the
    Rucio inventory; location is the dataset path (parent directory)."""
    token = _jlab_rucio_auth()
    names = []
    for root in ROOTS:
        found = _ndjson(_jlab_rucio_get(
            '/dids/epic/dids/search', token,
            type='file', created_after=SEARCH_EPOCH, name=root + '/*'))
        names.extend(n for n in found if isinstance(n, str))
    wanted = {}
    for name in names:
        segs = name.split('/')
        if len(segs) < 4 or not segs[2]:
            continue
        family = campaign_family(segs[2])
        if family in campaigns:
            wanted[name] = family
    print(f'inventory: {len(names)} files under roots, '
          f'{len(wanted)} in target campaigns')

    file_events = load_file_events()
    daily = {name: {} for name in campaigns}
    ordered = sorted(wanted)
    for start in range(0, len(ordered), BULK_CHUNK):
        chunk = ordered[start:start + BULK_CHUNK]
        for row in _bulkmeta(token, chunk):
            name = row.get('name')
            family = wanted.get(name)
            created = row.get('created_at')
            if not (family and created):
                continue
            day = dt.datetime.strptime(
                created, '%a, %d %b %Y %H:%M:%S %Z').date().isoformat()
            location = '/'.join(name.split('/')[:-1])
            slot = daily[family].setdefault(
                (location, day), {'files': 0, 'bytes': 0, 'events': 0,
                                  'unmeasured': 0})
            slot['files'] += 1
            slot['bytes'] += int(row.get('bytes') or 0)
            events = file_events.get(name)
            if events is None:
                slot['unmeasured'] += 1
            else:
                slot['events'] += events
        done = min(start + BULK_CHUNK, len(ordered))
        if done % 10000 < BULK_CHUNK:
            print(f'  bulkmeta {done}/{len(ordered)}')
    return daily


EVENTS_DB = '/data/wenauseic/swf-delivery/file_events.sqlite'


def load_file_events():
    """{file DID name: events} from the measurement store written by
    measure_file_events.py; empty (with a warning) when absent — the
    record then reports every file as unmeasured."""
    import sqlite3
    if not os.path.exists(EVENTS_DB):
        print(f'WARNING: no events store at {EVENTS_DB}; '
              f'events will read as unmeasured')
        return {}
    db = sqlite3.connect(EVENTS_DB)
    out = dict(db.execute('SELECT name, events FROM file_events'
                          ' WHERE events IS NOT NULL'))
    db.close()
    print(f'events store: {len(out)} files carry measured events')
    return out


def location_map(campaigns):
    """JLab dataset path -> (campaign, pc label), from task outputs."""
    mapping = {}
    for task in (ProdTask.objects
                 .filter(campaign__name__in=campaigns)
                 .select_related('dataset__physics_config', 'campaign')):
        if not (task.dataset_id and task.dataset.physics_config_id):
            continue
        pc = task.dataset.physics_config.label
        for output in task.outputs:
            did = str(output.get('did') or '')
            path = did.split(':', 1)[-1].strip('/')
            if path:
                mapping[path] = (task.campaign.name, pc)
    return mapping


def expected_map(campaigns):
    """pc label -> (expected, tier) per campaign, the current recorded
    denominator chain."""
    out = {}
    for name in campaigns:
        heads = list(Dataset.objects.filter(campaign__name=name)
                     .select_related('physics_config')
                     .order_by('composed_name', 'block_num', 'pk')
                     .distinct('composed_name'))
        projection = pc_request_projection(heads)
        per = {}
        for head in heads:
            if not head.physics_config_id:
                continue
            expected = head.expected_events
            tier = head.expected_events_source
            if expected is None:
                anchored = [r.nevents for r in
                            projection.get(head.composed_name, ())
                            if r.nevents]
                if anchored:
                    expected, tier = max(anchored), 'requested'
            per[head.physics_config.label] = (expected, tier or '')
        out[name] = per
    return out


def build_snaps(campaigns):
    daily = collect_files(campaigns)
    mapping = location_map(campaigns)
    expected = expected_map(campaigns)

    unmapped = {}
    per_day_pc = {}
    for family, slots in daily.items():
        for (location, day), counts in slots.items():
            mapped = mapping.get(location.strip('/'))
            if mapped is None:
                key = (family, location)
                unmapped[key] = unmapped.get(key, 0) + counts['files']
                continue
            campaign, pc = mapped
            slot = per_day_pc.setdefault(day, {}).setdefault(
                campaign, {}).setdefault(
                    pc, {'files': 0, 'bytes': 0, 'events': 0,
                         'unmeasured': 0})
            slot['files'] += counts['files']
            slot['bytes'] += counts['bytes']
            slot['events'] += counts.get('events', 0)
            slot['unmeasured'] += counts.get('unmeasured', 0)

    denominators_as_of = timezone.now().isoformat()
    snaps = []
    cumulative = {}
    for day in sorted(per_day_pc):
        day_leaves = per_day_pc[day]
        for campaign, leaves in day_leaves.items():
            camp_state = cumulative.setdefault(campaign, {})
            for pc, counts in leaves.items():
                slot = camp_state.setdefault(
                    pc, {'files': 0, 'bytes': 0, 'events': 0,
                         'unmeasured': 0})
                slot['files'] += counts['files']
                slot['bytes'] += counts['bytes']
                slot['events'] += counts['events']
                slot['unmeasured'] += counts['unmeasured']
        # The daily arrivals record: per PC, the day's registered
        # arrivals (the bumps) and the running cumulative — both on the
        # registered basis throughout, so the series never mixes bases
        # with the live placed-basis component (which feeds cards, not
        # curves).
        projection = {'campaigns': {}, 'backfill': {
            'denominators_as_of': denominators_as_of}}
        for campaign, camp_state in cumulative.items():
            leaves = {}
            totals = {'configs': 0, 'with_target': 0, 'events': 0,
                      'expected': 0, 'arrived_files': 0,
                      'arrived_events': 0, 'cum_files': 0,
                      'cum_bytes': 0, 'unmeasured_files': 0}
            day_counts = day_leaves.get(campaign, {})
            for pc, counts in camp_state.items():
                exp, tier = expected.get(campaign, {}).get(pc, (None, ''))
                day_slot = day_counts.get(pc, {})
                arrived = day_slot.get('files', 0)
                arrived_events = day_slot.get('events', 0)
                leaves[pc] = {'arrived_files': arrived,
                              'arrived_events': arrived_events,
                              'events': counts['events'],
                              'cum_files': counts['files'],
                              'cum_bytes': counts['bytes'],
                              'unmeasured_files': counts['unmeasured'],
                              'expected': exp, 'tier': tier}
                totals['configs'] += 1
                totals['arrived_files'] += arrived
                totals['arrived_events'] += arrived_events
                totals['events'] += counts['events']
                totals['cum_files'] += counts['files']
                totals['cum_bytes'] += counts['bytes']
                totals['unmeasured_files'] += counts['unmeasured']
                if exp is not None:
                    totals['with_target'] += 1
                    totals['expected'] += exp
            projection['campaigns'][campaign] = {
                'totals': totals, 'leaves': leaves}
        snaps.append((day, projection))
    return snaps, unmapped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaigns', default='26.06,26.07')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    campaigns = tuple(c.strip() for c in args.campaigns.split(',')
                      if c.strip())

    snaps, unmapped = build_snaps(campaigns)
    # Complete days only: the daily record runs through the end of
    # yesterday (UTC) and advances by rerun — the nightly path.
    cutoff = timezone.now().replace(hour=0, minute=0, second=0,
                                    microsecond=0)
    writable = [
        (day, projection) for day, projection in snaps
        if dt.datetime.fromisoformat(day + 'T23:59:59+00:00') < cutoff]

    print(f'\ndays reconstructed: {len(snaps)}, complete days through '
          f'yesterday: {len(writable)}')
    for day, projection in writable[-5:]:
        totals = {name: (block['totals']['cum_files'],
                         block['totals']['arrived_files'])
                  for name, block in projection['campaigns'].items()}
        print(f'  {day}: (cumulative, arrived) files {totals}')
    print(f'unmapped locations: {len(unmapped)} '
          f'({sum(unmapped.values())} files)')
    for (family, location), count in sorted(unmapped.items())[:10]:
        print(f'  {family} {location}: {count}')

    if not args.apply:
        print('\ndry run — nothing written; --apply writes the snaps')
        return
    removed = SystemSnap.objects.filter(
        scope='epicprod', capture_policy=CAPTURE_POLICY).delete()
    now = timezone.now()
    for day, projection in writable:
        snap_time = dt.datetime.fromisoformat(day + 'T23:59:59+00:00')
        SystemSnap.objects.create(
            scope='epicprod',
            snap_time=snap_time,
            observed_at=now,
            completed_at=now,
            snap_schema_version=1,
            capture_policy=CAPTURE_POLICY,
            encoding='full',
            reasons=['backfill'],
            changed_components=['delivery'],
            component_revisions={'delivery': 0},
            registration_versions={'delivery': 1},
            component_hashes={},
            state_hash='',
            state={'components': {'delivery': {
                'v': 1,
                'data': projection,
                'registration': DELIVERY_REGISTRATION,
                'revision': 0,
                'registration_version': 1,
                'assessed_at': snap_time.isoformat(),
                'source_as_of': snap_time.isoformat(),
                'accepted_at': now.isoformat(),
                'assessment_policy': ASSESSMENT_POLICY_VERSION,
                'publisher_identity': PUBLISHER_IDENTITY,
            }}},
        )
    print(f'\napplied: removed prior backfill {removed[0]}, '
          f'wrote {len(writable)} daily snaps')


if __name__ == '__main__':
    sys.exit(main())
