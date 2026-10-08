"""The live watch contract: the corun section and definition, the system
prompt, the mechanical floor, the artifact schema and the report
(docs/EPICPROD_ASSESSMENTS.md, The live watch). The harness pattern is
the segfault diagnosis's: deterministic evidence in, one fenced JSON
artifact out, validated here, one bounded repair run, quarantine on a
second failure.
"""
from swf_epicprod.segfault.spec import extract_artifact  # noqa: F401  (shared parser)

SCHEMA_VERSION = 1
DEFAULT_SECTION = 'epicprod.livewatch'
DEFAULT_BUNDLE_SECTION = 'epicprod.livewatch.bundle'
DEFINITION_NAME = 'live_watch'
SYSTEM_PROMPT_TITLE = 'epicprod live watch template'
WORKER_TIMEOUT_S = 30 * 60
WINDOW_HOURS = 4
VERDICTS = ('ok', 'attention', 'alarm')
JUDGMENTS = ('real_problem', 'expected', 'known_and_handled', 'unresolved')
RANK = {v: i for i, v in enumerate(VERDICTS)}

# The mechanical floor (raise-only for the model).
REPEAT_24H = 3      # one failure cause this many times in 24 h
FLAP_24H = 3        # this many failed-then-recovered turns in 24 h
DAYS_7D = 3         # one failure cause on this many distinct days in 7

SYSTEM_PROMPT = """\
You are a senior ePIC production operations expert watching the production
findings channel, epicprod-live, for the production lead. The channel is
for production findings, resolutions, operator actions, assessments,
arrivals and failures that matter; it is not a mirror of operational
logging. Your job is twofold: say what reached the channel that does not
belong there, and say which problems keep coming back without anyone
resolving them. The bundle in the prompt content is the evidence: the
channel's posts in the watch window and the last 24 hours, the complete
action record of failures and recoveries over 24 hours and 7 days grouped
by action, component and cause (maintenance passes included, which the
channel no longer shows), the flapping counts, the publication policy in
force, and the mechanical FLOOR with its reasons. Production code computes
the facts; your work is the judgment.

EXECUTION BUDGET — COMPLETE WITHIN 15 MINUTES OF WALL TIME. The worker
terminates the run at thirty minutes and a terminated run delivers
nothing. The submission carries submitted_at (UTC). Read the bundle first;
use tools only for what it cannot answer, bounded, one retry on a slow
service, then record the limitation. Stop investigating by the tenth
minute.

INVESTIGATION ROUTES (SWF Testbed MCP): epicprod_list_actions for one
action's records and their reasons; panda_study_job for a job the records
name; panda_list_jobs and panda_error_summary for the scope of a failure;
swf_get_system_state for the platform; the segfault and PCS tools where a
record points at them.

THE TASK.
1. Noise: each post in the window that does not belong in a findings
   channel (routine success, maintenance pass, duplicate, a mechanical
   step of a larger flow whose result is posted anyway). Name the action,
   why it does not belong, and the selection change that would keep it
   off (the action added to the maintenance set, a sublevel change, a
   condition on its outcome).
2. Recurring problems: each key in FLOOR.keys, under that exact key, and
   any other failure group you judge material. A key with a ~suffix is a
   failure group (one action, component and cause); a key without one is
   an action and component that failed and then recovered repeatedly
   (flapping), judged as a whole. Judge it: real_problem (a fault that costs
   production or data and has no resolution in the channel), expected (a
   designed retry or a known transient that clears), known_and_handled
   (a finding or resolution in the channel covers it; cite the post), or
   unresolved (the evidence cannot decide). For a real_problem state the
   cause as far as the evidence supports it, the scope, and a draft
   finding a person could post: the incident, the affected campaign,
   tasks or endpoint, what is known and uncertain, the evidence links,
   and the action or owner. A cause needs a live tool result or a record
   in the bundle; association is not causation.
3. Missing: a post the channel should have carried and did not (a real
   failure that was quieted, an assessment that never came).

VERDICT. ok when the channel is clean and nothing recurs unresolved;
attention for noise worth a selection change or a recurring real problem;
alarm for a real problem actively losing production or data. The FLOOR is
the minimum: you may raise it with justification, never lower it.

OUTPUT. Exactly one fenced json block and nothing else, this shape,
schema_version {schema_version}:

```json
{{
  "schema_version": {schema_version},
  "verdict": "ok | attention | alarm",
  "noise": [{{"action": "<action id>", "posts": <count>, "why": "...", "change": "<the selection change>"}}],
  "recurring": [{{"key": "<the group key from the bundle>", "judgment": "real_problem | expected | known_and_handled | unresolved",
                 "cause": "<as far as the evidence supports, or unresolved>", "scope": "...",
                 "evidence": ["<post url, action record or tool result>"],
                 "draft_finding": "<the post a person could make, for a real_problem; else empty>",
                 "owner": "<who acts>"}}],
  "missing": [{{"expected": "...", "why": "..."}}],
  "narration": "2-4 self-contained sentences for the production lead",
  "generation_report": {{
    "consulted": [{{"source": "<tool or document>", "contribution": "<what it gave>"}}],
    "investigation": [{{"claim": "...", "source": "<MCP server and tool>", "request": {{}}, "result": {{}}}}],
    "problems": ["<tool errors, gaps, workarounds>"],
    "unavailable": ["<what could not be obtained>"]
  }}
}}
```
"""


def system_prompt_text():
    return SYSTEM_PROMPT.format(schema_version=SCHEMA_VERSION)


def floor(groups, flaps):
    """The mechanical floor from the failure groups and the flapping
    counts. Pure. Returns (verdict, reasons)."""
    reasons = []
    for g in groups:
        if g.get('count_24h', 0) >= REPEAT_24H:
            reasons.append(f"{g['key']}: {g['count_24h']} failures in 24 h")
        elif g.get('days_7d', 0) >= DAYS_7D:
            reasons.append(f"{g['key']}: failing on {g['days_7d']} days of 7")
    for f in flaps:
        if f.get('turns_24h', 0) >= FLAP_24H:
            reasons.append(f"{f['key']}: failed then recovered {f['turns_24h']} times in 24 h")
    return ('attention' if reasons else 'ok'), reasons


def validate_artifact(artifact, bundle=None):
    """The schema and the floor; returns the list of problems."""
    problems = []
    if artifact.get('schema_version') != SCHEMA_VERSION:
        problems.append(f'schema_version must be {SCHEMA_VERSION}')
    if artifact.get('verdict') not in VERDICTS:
        problems.append('verdict must be ok, attention or alarm')
    floor_verdict = ((bundle or {}).get('floor') or {}).get('verdict', 'ok')
    if artifact.get('verdict') in RANK and RANK[artifact['verdict']] < RANK.get(floor_verdict, 0):
        problems.append(f'verdict {artifact["verdict"]} is below the floor {floor_verdict}')
    for key in ('noise', 'recurring', 'missing'):
        if not isinstance(artifact.get(key), list):
            problems.append(f'{key} must be a list')
    for n in artifact.get('noise') or []:
        if not isinstance(n, dict) or not all(isinstance(n.get(k), str) for k in ('action', 'why', 'change')):
            problems.append('each noise item needs action, why and change strings')
            break
    known_keys = {g.get('key') for g in (bundle or {}).get('failure_groups') or []}
    known_keys |= {f.get('key') for f in (bundle or {}).get('flapping') or []}
    for r in artifact.get('recurring') or []:
        if not isinstance(r, dict):
            problems.append('each recurring item must be an object')
            break
        if r.get('judgment') not in JUDGMENTS:
            problems.append(f"recurring {r.get('key')}: judgment must be one of {', '.join(JUDGMENTS)}")
        if known_keys and r.get('key') not in known_keys:
            problems.append(f"recurring {r.get('key')}: key is not a group in the bundle")
        if r.get('judgment') == 'real_problem' and not (r.get('draft_finding') or '').strip():
            problems.append(f"recurring {r.get('key')}: a real_problem needs a draft_finding")
        if not isinstance(r.get('evidence'), list):
            problems.append(f"recurring {r.get('key')}: evidence must be a list")
    floor_keys = {reason.split(':', 1)[0] for reason in ((bundle or {}).get('floor') or {}).get('reasons') or []}
    judged = {r.get('key') for r in artifact.get('recurring') or [] if isinstance(r, dict)}
    for k in sorted(floor_keys - judged):
        problems.append(f'the floor names {k}; recurring must judge it')
    if not isinstance(artifact.get('narration'), str) or not artifact['narration'].strip():
        problems.append('narration must be a non-empty string')
    gen = artifact.get('generation_report')
    if not isinstance(gen, dict):
        problems.append('generation_report must be an object')
    else:
        for key in ('consulted', 'investigation', 'problems', 'unavailable'):
            if not isinstance(gen.get(key), list):
                problems.append(f'generation_report.{key} must be a list')
    return problems


def validate_remainder(remainder):
    """The model emits the artifact and nothing else."""
    text = (remainder or '').strip()
    return ['the response carries text outside the json artifact'] if len(text) > 200 else []


def issue_set(artifact):
    """What the watch found, as a comparable set: the noise actions and the
    real problems by action and component. A run whose set equals the last
    registered one is not registered again (the assessments page carries
    changes, not every run); a new cause of an action already judged a real
    problem is not a new problem."""
    noise = sorted({n.get('action', '') for n in artifact.get('noise') or []})
    real = sorted({r.get('key', '').split('~', 1)[0] for r in artifact.get('recurring') or []
                   if r.get('judgment') == 'real_problem'})
    return {'verdict': artifact.get('verdict', 'ok'), 'noise': noise, 'real_problems': real}


def render_report(bundle, artifact):
    """The human report: the window and floor from the bundle, the
    judgment from the artifact, the generation report last."""
    fl = bundle.get('floor') or {}
    channel = bundle.get('channel') or {}
    lines = [
        f"# Live watch {bundle.get('window', {}).get('end', '')[:16]}: {artifact.get('verdict', '')}",
        '',
        artifact.get('narration', '').strip(),
        '',
        '## The window',
        '',
        f"- Channel posts: {len(channel.get('posts') or [])} in the last {bundle.get('channel_hours', 24)} h"
        f"{'' if channel.get('complete') else ' (read incomplete)'}",
        f"- Failure groups in 24 h: {sum(1 for g in bundle.get('failure_groups') or [] if g.get('count_24h'))}",
        f"- Floor: {fl.get('verdict', 'ok')}" + (': ' + '; '.join(fl.get('reasons') or []) if fl.get('reasons') else ''),
    ]
    if artifact.get('recurring'):
        lines += ['', '## Recurring', '']
        for r in artifact['recurring']:
            lines.append(f"- **{r.get('key', '')}** — {r.get('judgment', '')}: {r.get('cause', '')}"
                         f"{' (' + r['scope'] + ')' if r.get('scope') else ''}"
                         f"{'; owner ' + r['owner'] if r.get('owner') else ''}")
            if (r.get('draft_finding') or '').strip():
                lines.append(f"  - Draft finding: {r['draft_finding'].strip()}")
    if artifact.get('noise'):
        lines += ['', '## Noise in the channel', '']
        for n in artifact['noise']:
            lines.append(f"- {n.get('action', '')} ({n.get('posts', '')} posts): {n.get('why', '')} "
                         f"Change: {n.get('change', '')}")
    if artifact.get('missing'):
        lines += ['', '## Missing from the channel', '']
        for m in artifact['missing']:
            lines.append(f"- {m.get('expected', '')}: {m.get('why', '')}")
    gen = artifact.get('generation_report') or {}
    lines += ['', '### Generation report', '']
    for c in gen.get('consulted') or []:
        lines.append(f"- Consulted {c.get('source', '')}: {c.get('contribution', '')}")
    for inv in gen.get('investigation') or []:
        lines.append(f"- {inv.get('claim', '')} ({inv.get('source', '')})")
    for p in gen.get('problems') or []:
        lines.append(f'- Problem: {p}')
    for u in gen.get('unavailable') or []:
        lines.append(f'- Unavailable: {u}')
    return '\n'.join(lines) + '\n'
