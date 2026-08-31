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


def build_assembly_items(source_campaign):
    """One proposal item per source-campaign physics configuration.

    Defaults, in precedence:

    - a decided ``final`` propagation anywhere in the configuration's
      editions -> ``retire``;
    - a decided ``hold`` -> ``defer``;
    - an anchoring request with an event count -> ``include_requested``
      at the requested count;
    - delivered events on record -> ``include_prior`` at the recorded
      target, else the delivered count snapped to a round sample size;
    - otherwise -> ``defer``.

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
            disposition, plan_target = 'include_requested', req
        elif delivered > 0:
            plan_target = (target or snap_round(delivered)
                           or _round_2sig(delivered))
            disposition = 'include_prior'
        else:
            disposition, plan_target = 'defer', None
        items.append({
            'pc': pc,
            'disposition': disposition,
            'target_events': plan_target,
            'priority': row.get('priority'),
            'evidence': facts,
            'comment': f'{disposition}: {facts}',
        })
    return {'items': items, 'skipped': skipped,
            'source_campaign': source_campaign}


def propose_campaign_assembly(source_campaign, target_campaign, *,
                              created_by='', batch_id=''):
    """Generate and submit the assembly proposal set for
    ``target_campaign`` from ``source_campaign``'s record. Returns the
    propose-service result plus the item summary."""
    from ai.services import propose_campaign_plan

    built = build_assembly_items(source_campaign)
    result = propose_campaign_plan(
        target_campaign, built['items'],
        proposer=PROPOSER, batch_id=batch_id, created_by=created_by)
    result['built'] = len(built['items'])
    result['source_campaign'] = source_campaign
    return result
