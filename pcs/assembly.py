"""Campaign assembly: generating the plan-proposal set
(CONTINUOUS_PRODUCTION.md, Campaign assembly).

The generator derives one disposition proposal per physics
configuration of the source campaign, with defaults in the established
evidence tiers, and submits them through the AI proposal subsystem
(``ai.services.propose_campaign_plan``). Evidence and comments are
code-filled facts; no model judgment is involved in this proposer.
"""
import logging

from swf_epicprod.analytics.completion import (
    campaign_completion, campaign_heads, snap_round,
)

_log = logging.getLogger(__name__)

PROPOSER = 'campaign-assembly'


def _round_2sig(value):
    """Round to two significant digits — 1% granularity is enough for a
    plan target; a delivered count must not become an 8-digit target."""
    if not value or value <= 0:
        return value
    from math import floor, log10
    magnitude = 10 ** max(floor(log10(value)) - 1, 0)
    return int(round(value / magnitude) * magnitude)


def _added_pc_items(target_campaign, covered):
    """Configurations added via PCS beyond the source walk: an edition
    composed in the target campaign, or a configuration created after
    the target campaign row appeared. Proposed include with the target
    left open — the approval gate holds them until target and priority
    are set."""
    from .models import Campaign, PhysicsConfig

    campaign = Campaign.objects.filter(name=target_campaign).first()
    seen = {}
    for pc in PhysicsConfig.objects.filter(
            editions__campaign__name=target_campaign).distinct():
        seen[pc.label] = f'composed in {target_campaign} via PCS'
    if campaign is not None:
        for pc in PhysicsConfig.objects.filter(
                created_at__gte=campaign.created_at):
            seen.setdefault(
                pc.label,
                f'added in PCS {pc.created_at.date().isoformat()}')
    items = []
    for label in sorted(seen):
        if label in covered:
            continue
        facts = seen[label]
        items.append({
            'pc': label,
            'disposition': 'include',
            'target_events': None,
            'priority': None,
            'evidence': facts,
            'comment': f'include: {facts}',
        })
    return items


def build_assembly_items(source_campaign, target_campaign=None):
    """One proposal item per source-campaign physics configuration,
    plus configurations added via PCS for the target campaign
    (``_added_pc_items``).

    Defaults, in precedence:

    - a decided ``final`` propagation anywhere in the configuration's
      editions -> ``retire``;
    - a decided ``hold`` -> ``defer``;
    - an anchoring request with an event count -> ``include``
      at the requested count;
    - delivered events on record -> ``include`` at the recorded
      target, else the delivered count snapped to a round sample size;
    - otherwise -> ``include`` with the target left open.

    Priority comes from the completion record (the best request
    priority). Returns {'items': [...], 'skipped': {...}} — a
    configuration the completion record does not cover is skipped and
    counted, never guessed at.
    """
    from .services import pc_request_projection

    block = campaign_completion(source_campaign)
    if not block.get('available'):
        raise RuntimeError(
            f'no completion record for {source_campaign}: '
            f'{block.get("reason")}')

    heads = campaign_heads(source_campaign)
    projection = pc_request_projection(heads)
    requested = {}
    for head in heads:
        if not head.physics_config_id:
            continue
        label = head.physics_config.label
        values = [r.nevents for r in projection.get(head.composed_name, ())
                  if r.nevents]
        if values:
            requested[label] = max(requested.get(label, 0), max(values))
    decided = {}
    for head in heads:
        if head.physics_config_id and head.propagation != 'continue':
            decided.setdefault(head.physics_config.label,
                               set()).add(head.propagation)

    items = []
    skipped = {'no_basis': 0}
    for row in block['configurations']:
        pc = row['pc']
        delivered = int(row.get('delivered_events') or 0)
        target = row.get('target')
        req = requested.get(pc)
        states = decided.get(pc, set())
        facts = (f"{source_campaign}: {row.get('status') or 'no record'}, "
                 f"delivered {delivered:,}"
                 + (f" of target {target:,}" if target else '')
                 + (f"; requested {req:,}" if req else ''))
        if 'final' in states:
            disposition, plan_target = 'retire', None
            facts += '; decided final'
        elif 'hold' in states:
            disposition, plan_target = 'defer', None
            facts += '; decided hold'
        elif req:
            disposition, plan_target = 'include', req
        elif delivered > 0:
            plan_target = (target or snap_round(delivered)
                           or _round_2sig(delivered))
            disposition = 'include'
        else:
            # Not started is not a reason to drop the work: carry the
            # recorded target forward, or leave the target open — the
            # approval gate holds an include row until target and
            # priority are filled.
            disposition, plan_target = 'include', target or None
        items.append({
            'pc': pc,
            'disposition': disposition,
            'target_events': plan_target,
            'priority': row.get('priority'),
            'evidence': facts,
            'comment': f'{disposition}: {facts}',
        })
    if target_campaign:
        items.extend(_added_pc_items(target_campaign,
                                     {item['pc'] for item in items}))
    return {'items': items, 'skipped': skipped,
            'source_campaign': source_campaign}


def propose_campaign_assembly(source_campaign, target_campaign, *,
                              created_by='', batch_id=''):
    """Generate and submit the assembly proposal set for
    ``target_campaign`` from ``source_campaign``'s record. Returns the
    propose-service result plus the item summary."""
    from ai.services import propose_campaign_plan

    built = build_assembly_items(source_campaign, target_campaign)
    result = propose_campaign_plan(
        target_campaign, built['items'],
        proposer=PROPOSER, batch_id=batch_id, created_by=created_by)
    result['built'] = len(built['items'])
    result['source_campaign'] = source_campaign
    return result
