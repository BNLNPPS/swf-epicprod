"""Campaign completion: derived expected-events targets and the
completion estimate (CAMPAIGN_DELIVERY.md, Completion).

Two halves share one reading of the record:

- ``derive_expected_events(campaign)`` proposes a target for every
  physics configuration (PC) in the campaign that has none, from the
  evidence the record already holds, in rule precedence. The proposals
  are written through ``pcs.services.dataset_expected_events_set`` with
  source ``derived`` by ``scripts/derive_expected_events.py``; nothing
  here writes.
- ``campaign_completion(campaign)`` reads the recorded targets (every
  source) against delivered events from the daily delivery record and
  states the campaign's completion: per PC, PC-weighted, events-weighted,
  by physics category and by request priority, with the coverage of
  the denominator stated alongside — and the one-line summary.

Delivered events come from the newest ``delivery-daily-v1`` snap
(analytics/delivery_daily.py); event sums are floors where files are
unmeasured, and the block carries the unmeasured count.
"""

import collections

ROUND_LADDER = (10_000_000, 5_000_000, 4_000_000, 2_000_000, 1_000_000,
                500_000, 400_000, 200_000, 100_000)
ROUND_TOLERANCE = 0.03
COMPLETE_FRACTION = 0.97
IN_FLIGHT_STATES = {'running', 'paused', 'ready', 'scouting', 'assigning',
                    'registered', 'defined', 'pending', 'submitting',
                    'throttled', 'prepared'}
# A recorded target above this is not a sample size the campaign can
# mean; the derivation script clears such rows with a comment.
IMPLAUSIBLE_TARGET = 1_000_000_000


def snap_round(value):
    """The round sample size ``value`` closes on within the tolerance,
    else None."""
    if not value:
        return None
    for step in ROUND_LADDER:
        if (1 - ROUND_TOLERANCE) * step <= value <= (1 + ROUND_TOLERANCE) * step:
            return step
    return None


def daily_leaves():
    """{campaign: {pc label: leaf}} from the newest daily delivery snap,
    plus the snap time; empty when no snap exists."""
    from snapper_ai.models import SystemSnap

    snap = (SystemSnap.objects
            .filter(scope='epicprod', capture_policy='delivery-daily-v1')
            .order_by('-snap_time').first())
    if snap is None:
        return {}, None
    data = (((snap.state or {}).get('components') or {})
            .get('delivery') or {}).get('data') or {}
    return ({name: (block.get('leaves') or {})
             for name, block in (data.get('campaigns') or {}).items()},
            snap.snap_time)


def first_delivery_day(campaign_name):
    """The Eastern-Time day of the campaign's first recorded arrivals in
    the daily delivery record, else None."""
    from zoneinfo import ZoneInfo

    from snapper_ai.models import SystemSnap

    snaps = (SystemSnap.objects
             .filter(scope='epicprod', capture_policy='delivery-daily-v1')
             .order_by('snap_time')
             .only('snap_time', 'state'))
    for snap in snaps.iterator():
        block = ((((snap.state or {}).get('components') or {})
                  .get('delivery') or {}).get('data') or {})
        totals = ((block.get('campaigns') or {}).get(campaign_name) or {}
                  ).get('totals') or {}
        if totals.get('arrived_files') or totals.get('cum_files'):
            return snap.snap_time.astimezone(
                ZoneInfo('America/New_York')).date()
    return None


def campaign_heads(campaign_name):
    """Head rows of the campaign's editions, ordered as every target
    reader orders them (composed name, block, pk)."""
    from pcs.models import Dataset

    return list(Dataset.objects.filter(campaign__name=campaign_name)
                .select_related('physics_config',
                                'physics_config__physics_tag__category')
                .order_by('composed_name', 'block_num', 'pk')
                .distinct('composed_name'))


def pc_targets(heads):
    """pc label -> (expected, source) — the first edition head carrying
    a target wins; a PC with targetless editions beside a targeted one
    keeps the target. Every reader of the denominator uses this rule."""
    per = {}
    for head in heads:
        if not head.physics_config_id:
            continue
        label = head.physics_config.label
        if label in per:
            continue
        if head.expected_events is not None:
            per[label] = (head.expected_events,
                          head.expected_events_source or '')
    return per


def _pc_panda(campaign_name):
    """pc label -> {files, jobs, states} from the cached progress
    snapshot: output files delivered and PanDA jobs submitted, summed
    over the PC's tasks, with the task states seen."""
    from pcs.models import Campaign, ProdTask
    from pcs.services import load_campaign_progress_snapshot

    campaign = Campaign.objects.get(name=campaign_name)
    rows = (load_campaign_progress_snapshot(campaign) or {}).get('rows') or {}
    per = collections.defaultdict(
        lambda: {'files': 0, 'jobs': 0, 'states': set()})
    tasks = (ProdTask.objects.filter(campaign=campaign)
             .exclude(status='past_output')
             .select_related('dataset__physics_config'))
    for task in tasks:
        if not task.dataset.physics_config_id:
            continue
        row = rows.get(str(task.pk)) or {}
        for output in row.get('outputs') or []:
            processing = output.get('processing') or {}
            if not processing.get('status'):
                continue
            slot = per[task.dataset.physics_config.label]
            slot['states'].add(str(processing['status']))
            try:
                slot['jobs'] += int(processing.get('total_jobs')
                                    or output.get('expected_jobs') or 0)
                slot['files'] += int(output.get('file_count') or 0)
            except (TypeError, ValueError):
                continue
    return per


def _prior_campaigns(campaign_name, leaves):
    """Earlier campaign families present in the daily record, newest
    first — the prior-campaign rule's evidence order."""
    def key(name):
        return tuple(int(p) if p.isdigit() else p for p in name.split('.'))
    return sorted((n for n in leaves if n != campaign_name
                   and key(n) < key(campaign_name)), key=key, reverse=True)


def derive_expected_events(campaign_name):
    """Proposed derived targets for the campaign's PCs without one.

    Returns ``{'proposals': [...], 'implausible': [...], 'skipped': {...}}``.
    Each proposal: pc, name (the edition head the target is written
    on — the campaign-versioned edition when one exists), value, rule,
    evidence (the comment text). Rules, in precedence:

    R1 round closure — delivered events close on a round sample size
       and no task is in flight: the round number.
    R2 prior campaign — the same PC's delivered events in the newest
       earlier campaign in the record, snapped to a round size.

    A PC matching no rule is left without a target; the completion
    estimate reports it as uncovered. PanDA job counts are not a
    basis: a task's job total includes every retry, so a task that
    delivered one file from thousands of attempts would derive a
    target thousands of times its sample size.
    """
    leaves, _snap_time = daily_leaves()
    mine = leaves.get(campaign_name) or {}
    priors = _prior_campaigns(campaign_name, leaves)
    heads = campaign_heads(campaign_name)
    targets = pc_targets(heads)
    panda = _pc_panda(campaign_name)

    by_pc = collections.defaultdict(list)
    for head in heads:
        if head.physics_config_id:
            by_pc[head.physics_config.label].append(head)

    def write_head(pc_heads):
        for head in pc_heads:
            if head.detector_version.startswith(campaign_name):
                return head
        return pc_heads[0]

    proposals = []
    implausible = []
    skipped = collections.Counter()
    for pc, pc_heads in sorted(by_pc.items()):
        target = targets.get(pc)
        if target is not None:
            if target[0] >= IMPLAUSIBLE_TARGET:
                for head in pc_heads:
                    if head.expected_events is not None:
                        implausible.append({
                            'pc': pc, 'name': head.composed_name,
                            'value': head.expected_events,
                            'source': head.expected_events_source})
            else:
                skipped['already targeted'] += 1
            continue
        leaf = mine.get(pc) or {}
        delivered = int(leaf.get('events') or 0)
        files = int(leaf.get('cum_files') or 0)
        pj = panda.get(pc)
        in_flight = bool(pj and (pj['states'] & IN_FLIGHT_STATES))
        head = write_head(pc_heads)
        value = rule = evidence = None
        closure = snap_round(delivered)
        if delivered and closure and not in_flight:
            value, rule = closure, 'R1'
            evidence = (f'derived R1 round closure: delivered {delivered:,} '
                        f'events close on {closure:,}, no task in flight')
        else:
            for prior in priors:
                prior_events = int((leaves[prior].get(pc) or {})
                                   .get('events') or 0)
                if prior_events:
                    value = snap_round(prior_events) or prior_events
                    rule = 'R2'
                    evidence = (f'derived R2 prior campaign: {prior} '
                                f'delivered {prior_events:,} events'
                                + (f', snapped to {value:,}'
                                   if value != prior_events else ''))
                    break
        if value is None:
            if not delivered and not files:
                skipped['not started, no evidence'] += 1
            else:
                skipped['delivering, no rule applies'] += 1
            continue
        proposals.append({'pc': pc, 'name': head.composed_name,
                          'value': value, 'rule': rule,
                          'evidence': evidence, 'delivered': delivered})
    return {'proposals': proposals, 'implausible': implausible,
            'skipped': dict(skipped)}


def _pc_priorities(heads):
    """pc label -> best (lowest number) request priority among the
    requests anchored to the PC, else None."""
    from pcs.services import pc_request_projection

    projection = pc_request_projection(heads)
    per = {}
    for head in heads:
        if not head.physics_config_id:
            continue
        for request in projection.get(head.composed_name, ()):
            if request.priority is None or request.priority < 0:
                continue
            label = head.physics_config.label
            per[label] = min(per.get(label, request.priority),
                             request.priority)
    return per


def campaign_completion(campaign_name):
    """The completion estimate block for one campaign; see module doc."""
    leaves, snap_time = daily_leaves()
    mine = leaves.get(campaign_name) or {}
    heads = campaign_heads(campaign_name)
    if not heads:
        return {'available': False,
                'reason': f'no editions recorded for campaign {campaign_name}'}
    if snap_time is None:
        return {'available': False,
                'reason': 'no daily delivery record; the nightly '
                          'delivery_daily_rebuild step builds it'}
    targets = pc_targets(heads)
    panda = _pc_panda(campaign_name)
    priorities = _pc_priorities(heads)

    category_of = {}
    for head in heads:
        if head.physics_config_id:
            tag = head.physics_config.physics_tag
            category_of.setdefault(
                head.physics_config.label,
                tag.category.name if tag and tag.category_id else 'Uncategorized')

    pcs = []
    for pc in sorted(category_of):
        leaf = mine.get(pc) or {}
        delivered = int(leaf.get('events') or 0)
        files = int(leaf.get('cum_files') or 0)
        target = targets.get(pc)
        pj = panda.get(pc)
        in_flight = bool(pj and (pj['states'] & IN_FLIGHT_STATES))
        if target is not None and target[0] > 0:
            fraction = min(delivered / target[0], 1.0)
            basis = target[1] or 'target'
        elif not delivered and not files and not in_flight:
            fraction, basis = 0.0, 'not started'
        else:
            fraction, basis = None, 'no target'
        pcs.append({
            'pc': pc, 'category': category_of[pc],
            'priority': priorities.get(pc),
            'delivered_events': delivered, 'files': files,
            'bytes': int(leaf.get('cum_bytes') or 0),
            'unmeasured_files': int(leaf.get('unmeasured_files') or 0),
            'target': target[0] if target else None,
            'target_source': target[1] if target else '',
            'basis': basis, 'completion': fraction,
            'in_flight': in_flight,
        })

    def rollup(rows):
        covered = [r for r in rows if r['completion'] is not None]
        targeted = [r for r in rows if r['target']]
        den = sum(r['target'] for r in targeted)
        num = sum(min(r['delivered_events'], r['target']) for r in targeted)
        return {
            'configurations': len(rows),
            'covered': len(covered),
            'targeted': len(targeted),
            'complete': sum(1 for r in covered
                            if r['completion'] >= COMPLETE_FRACTION),
            'not_started': sum(1 for r in rows if r['basis'] == 'not started'),
            'no_target': sum(1 for r in rows if r['basis'] == 'no target'),
            'in_flight': sum(1 for r in rows if r['in_flight']),
            'fraction_pc': (round(sum(r['completion'] for r in covered)
                                  / len(covered), 4) if covered else None),
            'fraction_events': round(num / den, 4) if den else None,
            'target_events': den,
            'delivered_events': sum(r['delivered_events'] for r in rows),
            'files': sum(r['files'] for r in rows),
            'bytes': sum(r['bytes'] for r in rows),
            'unmeasured_files': sum(r['unmeasured_files'] for r in rows),
        }

    overall = rollup(pcs)
    since = first_delivery_day(campaign_name)
    overall['delivered_since'] = since.isoformat() if since else None
    by_source = collections.Counter(r['target_source'] for r in pcs
                                    if r['target'])
    by_category = {name: rollup([r for r in pcs if r['category'] == name])
                   for name in sorted({r['category'] for r in pcs})}
    by_priority = {}
    for level in sorted({r['priority'] for r in pcs
                         if r['priority'] is not None}):
        by_priority[str(level)] = rollup(
            [r for r in pcs if r['priority'] == level])
    unprioritized = [r for r in pcs if r['priority'] is None]
    if unprioritized:
        by_priority['none'] = rollup(unprioritized)

    return {
        'available': True,
        'campaign': campaign_name,
        'record_as_of': snap_time.isoformat(),
        'method': (
            'per configuration min(delivered events / target, 1); '
            'fraction_pc = mean over configurations with a target or '
            'not started (counted 0); fraction_events = sum of capped '
            'delivered over sum of targets; configurations delivering '
            'without a target are excluded and counted in no_target; '
            'delivered event sums are floors where files are unmeasured'),
        'overall': overall,
        'targets_by_source': dict(by_source),
        'by_category': by_category,
        'by_priority': by_priority,
        'line': completion_line(campaign_name, overall, by_source),
        'configurations': pcs,
    }


def _fmt_events(value):
    if value >= 1_000_000_000:
        return f'{value / 1e9:.2f}G'
    if value >= 1_000_000:
        return f'{value / 1e6:.0f}M'
    return f'{value:,}'


def delivered_summary(overall):
    """The delivered-totals summary, e.g.
    'Delivered since Jul 13: 501M events, 286 TB'."""
    tb = overall['bytes'] / 1e12
    since = overall.get('delivered_since')
    if since:
        import datetime as _dt
        day = _dt.date.fromisoformat(since)
        since_text = f'Delivered since {day:%b} {day.day}'
    else:
        since_text = 'Delivered'
    return (f'{since_text}: {_fmt_events(overall["delivered_events"])} '
            f'events, {tb:.0f} TB')


def completion_line(campaign_name, overall, by_source):
    """The one-line campaign summary."""
    fraction = overall.get('fraction_pc')
    if fraction is None:
        head = f'{campaign_name}: completion not estimable (no targets)'
    else:
        sources = ' and '.join(
            f'{count} {label}' for label, count in (
                ('with campaign-included count',
                 by_source.get('included', 0)),
                ('with explicitly requested count',
                 by_source.get('requested', 0)),
                ('derived (guessed)', by_source.get('derived', 0)))
            if count)
        # The line breaks once, after the covered population, so the
        # basis and the totals read as two lines on a page.
        head = (f'{campaign_name}: ~{round(100 * fraction)}% complete, '
                f'the mean completion over {overall["covered"]} of the '
                f'campaign\'s {overall["configurations"]} physics '
                f'configurations: {overall["targeted"]} with an event '
                f'count target, {sources}, plus {overall["not_started"]} '
                f'not started;\n{overall["no_target"]} physics '
                f'configurations have delivered data but no event count '
                f'target, so their completion is unknown and they are '
                f'left out of the average')
    return (f'{head} · {overall["complete"]} physics configurations '
            f'complete · {delivered_summary(overall)}')
