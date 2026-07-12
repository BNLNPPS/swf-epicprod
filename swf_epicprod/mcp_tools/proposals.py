"""MCP surface for AI proposals (AI_PROPOSALS.md): the bot review flow.

One flat list tool over every proposal category, and one decide tool that
relays a human's yes/no. All differentiated complexity — validation,
revalidation, execution, logging — is the deterministic proposal
machinery in ``ai.services``; the calling LLM contributes no
interpretation.
"""

from asgiref.sync import sync_to_async

from monitor_app.mcp import mcp
from monitor_app.mcp.common import _monitor_url

# The label a human reads for each proposal category.
ACTION_LABELS = {'propagation': 'campaign propagation'}

# SysConfig key: usernames allowed to decide proposals through this MCP
# surface (the bot relay). Default empty — MCP decides are refused until
# an operator lists approvers on the System page.
MCP_APPROVERS_KEY = 'ai_proposal_mcp_approvers'


def _proposal_line(row):
    payload = row.payload or {}
    pre = row.precondition or {}
    change = f"{pre.get('prev_state', '?')} -> {payload.get('state', '?')}"
    if payload.get('replaced_by'):
        change += f" (replaced by {payload['replaced_by']})"
    label = ACTION_LABELS.get(row.action, row.action)
    origin = ', '.join(x for x in (
        row.proposer, row.batch_id,
        row.created_at.strftime('%Y-%m-%d') if row.created_at else '',
    ) if x)
    return (f"{row.ref}  {label} on {row.subject_key}: {change} — "
            f"{row.comment} [{origin}]")


def _list_proposals_sync(status, limit):
    from ai.models import Proposal

    valid = {s for s, _ in Proposal.STATUS_CHOICES} | {'all'}
    status = (status or 'proposed').strip()
    if status not in valid:
        return {'success': False,
                'error': f"status must be one of {sorted(valid)}; got {status!r}"}

    qs = Proposal.objects.all()
    if status != 'all':
        qs = qs.filter(status=status)
    total = qs.count()
    safe_limit = max(1, min(int(limit or 50), 200))
    rows = list(qs.order_by('-created_at')[:safe_limit])

    items = [{
        'ref': r.ref,
        'status': r.status,
        'subject': r.subject_key,
        'change': _proposal_line(r).split(': ', 1)[-1],
        'comment': r.comment,
        'proposer': r.proposer,
        'batch_id': r.batch_id,
        'proposed_at': r.created_at.isoformat() if r.created_at else None,
    } for r in rows]

    if status == 'proposed':
        header = (f'{total} AI proposal(s) awaiting decision:'
                  if total else 'No AI proposals awaiting decision.')
    else:
        header = f'{total} AI proposal(s) with status {status}:'
    display = '\n'.join([header] + [_proposal_line(r) for r in rows])

    return {
        'success': True,
        'total_count': total,
        'shown': len(rows),
        'status': status,
        'display': display,
        'items': items,
        'monitor_urls': [
            {'title': 'AI proposal list', 'url': _monitor_url('/ai/proposals/')},
        ],
    }


def _decide_proposal_sync(ref, decision, username, quality):
    from monitor_app.models import SysConfig

    from ai import services
    from pcs.services import ServiceError

    username = (username or '').strip()
    if not username:
        return {'success': False,
                'error': 'username (the deciding human) is required'}
    approvers = SysConfig.get_setting(MCP_APPROVERS_KEY, [])
    if username not in (approvers or []):
        return {
            'success': False,
            'error': (f'{username!r} is not an approved proposal decider on '
                      f'this surface. Approvers are listed in SysConfig key '
                      f'{MCP_APPROVERS_KEY!r} (System page); current list: '
                      f'{approvers or []}. The web AI proposal list remains '
                      f'available to any signed-in user.'),
        }
    try:
        row = services.parse_proposal_ref(ref)
        if row.status != 'proposed':
            return {'success': False,
                    'error': f'{row.ref} is not pending — its status is '
                             f'{row.status!r}; nothing to decide'}
        result = services.proposal_decide(
            [], decision, decided_by=username, quality=quality or '',
            filter_state=f'mcp:{row.ref}', proposal_ids=[row.pk])
    except ServiceError as e:
        return {'success': False, 'error': e.detail}

    if result.get('stale'):
        outcome = (f'{row.ref} was STALE: the record changed since the '
                   f'proposal saw it, so it was withdrawn, not executed.')
    elif decision == 'approve':
        outcome = (f'{row.ref} approved by {username} and executed: '
                   f'{row.subject_key} '
                   f"-> {(row.payload or {}).get('state', '?')}.")
    else:
        outcome = f'{row.ref} denied by {username}.'
        if quality:
            outcome += f' (quality: {quality})'
    return {'success': True, 'ref': row.ref, 'decision': decision,
            'outcome': outcome, 'result': result}


@mcp.tool()
async def ai_list_proposals(status: str = 'proposed', limit: int = 50) -> dict:
    """
    List AI proposals — concrete actions an AI has proposed that await a
    human's approve/deny (AI_PROPOSALS.md).

    THE BOT FLOW, exactly three steps:
    1. Call with no arguments to get the outstanding proposals.
    2. Show the returned `display` text to the human VERBATIM — it is
       preformatted, one line per proposal, each starting with its ref
       (e.g. 'cp-12').
    3. When the human answers with a ref and a verdict ('approve cp-12',
       'deny cp-12'), relay it with ai_decide_proposal. Never decide
       without an explicit human instruction naming the ref, and never
       invent or guess a ref.

    Args:
        status: 'proposed' (default: awaiting decision), or 'executed',
            'denied', 'withdrawn', 'stale', 'all' for history.
        limit: Max proposals returned (default 50, cap 200).

    Returns:
        display (preformatted text to show the human), total_count, items
        (structured: ref, status, subject, change, comment, proposer,
        batch_id, proposed_at), and the web AI proposal list URL.
    """
    return await sync_to_async(_list_proposals_sync)(status=status,
                                                     limit=limit)


@mcp.tool()
async def ai_decide_proposal(ref: str, decision: str,
                             username: str, quality: str = '') -> dict:
    """
    Relay a human's decision on one AI proposal: approve executes the
    frozen payload through the deterministic service path, deny records
    the refusal (AI_PROPOSALS.md). This tool transmits the human's yes/no
    — nothing more.

    RULES for the calling LLM:
    - Only call after an explicit human instruction naming the ref
      ('approve cp-12'). One call per ref, exactly as instructed.
    - username is the deciding HUMAN's monitor username — never the bot's
      own name. Deciders must be on the SysConfig approver list
      ('ai_proposal_mcp_approvers'); anyone else gets a refusal, which you
      report back verbatim.
    - Report the returned `outcome` to the human verbatim.
    - A garbled ref is refused, never reinterpreted — on refusal, show the
      error and re-list with ai_list_proposals.

    Args:
        ref: The proposal ref exactly as listed, e.g. 'cp-12'.
        decision: 'approve' or 'deny'.
        username: The deciding human's monitor username.
        quality: Optional review tag (wrong | poor | ok | good) on a deny;
            'wrong' marks the proposal miscalibrated and weighs against
            the proposer's track record.

    Returns:
        outcome (text for the human), ref, decision, and the decide
        result (approved/denied/stale lists).
    """
    return await sync_to_async(_decide_proposal_sync)(
        ref=ref, decision=decision, username=username, quality=quality)
