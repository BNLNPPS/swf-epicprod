"""Build the campaign delivered-data daily record from JLab Rucio
(CAMPAIGN_DELIVERY.md, Ongoing production).

The Snapper campaign view's curves draw from one daily registered-basis
record: per ET calendar day and physics configuration, the day's
arrived files/bytes/events and the running cumulative, keyed to PCs
through the PCS task output records, events joined from the measurement
store (analytics/file_events.py). Each build is a full idempotent
reconstruction from the complete Rucio file inventory: prior daily
snaps are removed and every complete ET day through yesterday is
rewritten, so newly measured events and newly mapped locations refine
the whole history on every run. One snap per day, stamped at ET day
end, capture policy ``delivery-daily-v1``.

Runs nightly as a ``catalog_sync`` chain step (the prod-ops agent's
``delivery-daily-rebuild.py`` doer); runnable by hand through
``scripts/backfill_delivery_history.py``.
"""

import datetime as dt
import json
import ssl
import urllib.request
from zoneinfo import ZoneInfo

ROOTS = ('/RECO', '/SIMU')
SEARCH_EPOCH = '2026-01-01T00:00:00'
BULK_CHUNK = 500
CAPTURE_POLICY = 'delivery-daily-v1'
# Prior generations removed on every rebuild: the original hand-run
# backfill label and the current one.
REPLACED_POLICIES = ('backfill-v1', CAPTURE_POLICY)
ET = ZoneInfo('America/New_York')


def _bulkmeta(token, names, scope='epic'):
    from pcs.services import JLAB_RUCIO_URL

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


def target_campaigns():
    """Campaigns the daily record covers: every campaign already in
    the record (recorded history is never dropped), the current and
    last lifecycle slots, and any campaign currently producing (fresh
    Rucio arrivals). Bounded by construction: the metadata pass scales
    with active campaigns, not the full catalog."""
    from pcs.models import Campaign
    from snapper_ai.models import SystemSnap

    from .rollup import producing_campaigns

    names = set(Campaign.objects
                .filter(lifecycle__in=('current', 'last'))
                .values_list('name', flat=True))
    names.update(camp.name for camp, _ in producing_campaigns())
    newest = (SystemSnap.objects
              .filter(scope='epicprod',
                      capture_policy__in=REPLACED_POLICIES)
              .order_by('-snap_time').first())
    if newest:
        data = (((newest.state or {}).get('components') or {})
                .get('delivery') or {}).get('data') or {}
        names.update((data.get('campaigns') or {}).keys())
    return tuple(sorted(names))


def load_file_events():
    """{file DID name: events} from the measurement store written by
    analytics/file_events.py; empty (with a warning) when absent — the
    record then reports every file as unmeasured."""
    import os
    import sqlite3

    from .file_events import DEFAULT_DB

    if not os.path.exists(DEFAULT_DB):
        print(f'WARNING: no events store at {DEFAULT_DB}; '
              f'events will read as unmeasured')
        return {}
    db = sqlite3.connect(DEFAULT_DB)
    out = dict(db.execute('SELECT name, events FROM file_events'
                          ' WHERE events IS NOT NULL'))
    db.close()
    print(f'events store: {len(out)} files carry measured events')
    return out


def collect_files(campaigns, limit_files=0):
    """{(location, ET day): {'files','bytes','events','unmeasured'}}
    per campaign from the Rucio inventory, plus {unknown family: file
    count} for files whose campaign has no catalog row — reported,
    never dropped silently. ``limit_files`` caps the metadata pass for
    fast validation runs; a capped build is partial by construction."""
    from pcs.services import (_jlab_rucio_auth, _jlab_rucio_get, _ndjson,
                              campaign_family)

    token = _jlab_rucio_auth()
    # One search per root and target campaign family: the pattern
    # covers the family's patch-level versions, and the stream carries
    # only the target campaigns' files instead of the full roots.
    names = []
    for root in ROOTS:
        for family in campaigns:
            found = _ndjson(_jlab_rucio_get(
                '/dids/epic/dids/search', token,
                type='file', created_after=SEARCH_EPOCH,
                name=f'{root}/{family}*'))
            names.extend(n for n in found if isinstance(n, str))
    wanted, unknown = {}, {}
    for name in names:
        segs = name.split('/')
        if len(segs) < 4 or not segs[2]:
            continue
        family = campaign_family(segs[2])
        if family in campaigns:
            wanted[name] = family
        else:
            unknown[family] = unknown.get(family, 0) + 1
    print(f'inventory: {len(names)} files under roots, '
          f'{len(wanted)} in catalog campaigns')

    file_events = load_file_events()
    daily = {name: {} for name in campaigns}
    ordered = sorted(wanted)
    if limit_files:
        ordered = ordered[:int(limit_files)]
        print(f'capped at {len(ordered)} of {len(wanted)} files '
              f'(validation run)')
    for start in range(0, len(ordered), BULK_CHUNK):
        chunk = ordered[start:start + BULK_CHUNK]
        for row in _bulkmeta(token, chunk):
            name = row.get('name')
            family = wanted.get(name)
            created = row.get('created_at')
            if not (family and created):
                continue
            # Rucio created_at is UTC; the production day is the ET
            # calendar day (the system's ET time contract).
            registered = dt.datetime.strptime(
                created, '%a, %d %b %Y %H:%M:%S %Z').replace(
                    tzinfo=dt.timezone.utc)
            day = registered.astimezone(ET).date().isoformat()
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
    return daily, unknown


def location_map(campaigns):
    """JLab dataset path -> (campaign, pc label), from task outputs."""
    from pcs.models import ProdTask

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
    from pcs.models import Dataset
    from pcs.services import pc_request_projection

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


def build_snaps(campaigns, limit_files=0):
    from django.utils import timezone

    daily, unknown = collect_files(campaigns, limit_files)
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
    # Every ET day from first arrival through today, quiet days
    # included: a zero-arrival day writes its snap (arrivals zero,
    # cumulative carried forward) so the record honors its
    # every-complete-day contract and the campaign view's daily columns
    # never widen over a gap. The caller's writable filter trims today.
    all_days = []
    if per_day_pc:
        day_cursor = dt.date.fromisoformat(min(per_day_pc))
        last_day = max(dt.date.fromisoformat(max(per_day_pc)),
                       timezone.now().astimezone(ET).date())
        while day_cursor <= last_day:
            all_days.append(day_cursor.isoformat())
            day_cursor += dt.timedelta(days=1)
    for day in all_days:
        day_leaves = per_day_pc.get(day, {})
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
    return snaps, unmapped, unknown


def rebuild_delivery_daily(campaigns=None, *, apply=False, created_by='',
                           limit_files=0):
    """Reconstruct the daily delivery record; complete ET days through
    yesterday. Dry run unless ``apply``. Returns a summary dict; every
    anomaly (unmapped locations, unknown campaign families) is in it,
    counted, never silently dropped. ``limit_files`` caps the metadata
    pass for fast validation and is dry-run only: a capped build is
    partial and must never replace the record."""
    from django.utils import timezone

    from monitor_app.snapper_delivery import (ASSESSMENT_POLICY_VERSION,
                                              DELIVERY_REGISTRATION,
                                              PUBLISHER_IDENTITY)
    from snapper_ai.models import SystemSnap

    if limit_files and apply:
        raise ValueError('limit_files is validation-only: refusing to '
                         'apply a partial rebuild')
    campaigns = tuple(campaigns) if campaigns else target_campaigns()
    snaps, unmapped, unknown = build_snaps(campaigns, limit_files)

    today_et = timezone.now().astimezone(ET).date()
    writable = [(day, projection) for day, projection in snaps
                if dt.date.fromisoformat(day) < today_et]

    print(f'days reconstructed: {len(snaps)}, complete ET days through '
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
    if unknown:
        print(f'files in campaigns with no catalog row: {unknown}')

    summary = {
        'campaigns': sorted(campaigns),
        'days': len(snaps), 'writable_days': len(writable),
        'unmapped_locations': len(unmapped),
        'unmapped_files': sum(unmapped.values()),
        'unknown_families': unknown, 'applied': bool(apply),
        'removed': 0, 'written': 0,
    }
    if writable:
        # The newest recorded day's arrivals, so the caller (the ops
        # agent's Capcom notice) can state the event without a DB read.
        day, projection = writable[-1]
        summary['newest_day'] = day
        summary['newest_arrivals'] = {
            name: {'files': (block['totals'] or {}).get('arrived_files', 0),
                   'events': (block['totals'] or {}).get('arrived_events', 0)}
            for name, block in projection['campaigns'].items()}
    if not apply:
        print('dry run — nothing written')
        return summary

    now = timezone.now()
    removed = SystemSnap.objects.filter(
        scope='epicprod', capture_policy__in=REPLACED_POLICIES).delete()
    for day, projection in writable:
        date = dt.date.fromisoformat(day)
        snap_time = dt.datetime(date.year, date.month, date.day,
                                23, 59, 59, tzinfo=ET)
        SystemSnap.objects.create(
            scope='epicprod',
            snap_time=snap_time,
            observed_at=now,
            completed_at=now,
            snap_schema_version=1,
            capture_policy=CAPTURE_POLICY,
            encoding='full',
            reasons=['daily-rebuild'],
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
    summary['removed'] = removed[0]
    summary['written'] = len(writable)
    print(f'applied: removed {removed[0]} prior daily snaps, '
          f'wrote {len(writable)}')
    return summary
