"""Assessment content and contract: templates, artifact schema, validation.

Pure stdlib — importable by the stdlib-only trigger and the Django
enforcement doer alike. This module is the operative source of the
prompt templates; ``bootstrap`` pushes them to corun-ai as the versioned
SystemPrompt a JobDefinition references. The artifact schema here is
what ``enforce`` accepts; the doc copy (EPICPROD_ASSESSMENTS_V1.md)
describes it.
"""

import json
import re

SCHEMA_VERSION = 1
VERDICTS = ('ok', 'attention', 'alarm')
AXES = ('arrivals', 'processing', 'failures', 'dispositions', 'infrastructure')

# corun-side names (bootstrap creates them; the trigger and enforcement
# reference them through the environment so a rename never hides here).
DEFAULT_SECTION = 'epicprod.assessment'
DEFAULT_DEFINITION_NAME = 'campaign_assessment'
DEFAULT_SYSTEM_PROMPT_TITLE = 'epicprod campaign assessment template'

NIGHTLY_TEMPLATE = """\
You are the {kind} production assessor for ePIC campaign {campaign},
assessment date {date}. The prompt carries your evidence bundle:

1. CAMPAIGN NARRATIVE — what this campaign is for and what should be
   running.
2. GENERAL NARRATIVE — standing facts of how production operates.
3. PRIOR ASSESSMENTS — your recent artifacts for this campaign and kind.
4. BASE EVIDENCE — the campaign status rollup (progress, PanDA health,
   arrivals, dispositions, action-stream activity, credentials), which
   carries the mechanical verdict FLOOR, and system status.
5. THE BUNDLE MANIFEST — what the harness fetched, with per-call
   outcomes. A failed fetch is degraded evidence: treat and report it
   as such.

You also hold the swf-testbed MCP toolset (panda_*, pcs_*, epicprod_*,
Rucio) for drill-down.

TASK 1 — build the comprehensive picture, from the campaign on down.
Read every summary in the bundle as a surfacing instrument, not a
confirmation of any other. Extend the picture wherever anything
surfaced warrants it: drill down to the task, the site, the error, the
action behind it (panda_diagnose_jobs, panda_study_job,
pcs_prodtask_get, Rucio tools), and call any further summaries you
need. Directed digging within your time budget, in service of the
verdict and top issues.

TASK 2 — reason over the picture and report: correlation across signals
(an arrivals dip, one site's error spike, and a queue alarm may be one
event, not three), root causes your investigation established,
deviation from the narrative's stated intent, trend inflection against
your prior assessments, and the explicit call on what requires human
action, if anything. Signals you examined and set aside go in the
dismissed list with reasons — tomorrow's run inherits your
explanations. Do not restate chart or table contents. A quiet night is
a valid result: say so briefly.

You interpret and investigate; you do not calculate. Every number you
state must have arrived in the bundle or a tool result during this run;
it is verified. The FLOOR is your minimum verdict — raise it with
justification if warranted, never lower it.

ALWAYS close with your generation report: what you consulted and what
each contributed, tool errors or gaps you hit, anything you could not
obtain, and workarounds you took. Candour here is prized — a degraded
run must read as degraded, never as smooth.

OUTPUT FORMAT — exactly two parts, in this order:
1. One fenced ```json block with EXACTLY this shape — these field names,
   these five axis keys, no others:

   {{
     "schema_version": {schema_version},
     "verdict": "<ok|attention|alarm>",
     "axes": {{
       "arrivals":       {{"status": "<ok|attention|alarm>", "note": "<short>"}},
       "processing":     {{"status": "<ok|attention|alarm>", "note": "<short>"}},
       "failures":       {{"status": "<ok|attention|alarm>", "note": "<short>"}},
       "dispositions":   {{"status": "<ok|attention|alarm>", "note": "<short>"}},
       "infrastructure": {{"status": "<ok|attention|alarm>", "note": "<short>"}}
     }},
     "key_metrics": [
       {{"name": "<metric>", "value": "<as it appeared in evidence>",
         "delta": "<vs prior, or empty>", "ref": "<source>"}}
     ],
     "top_issues": [
       {{"title": "<issue>", "severity": "<attention|alarm>",
         "evidence": ["<refs>"], "action": "<what a human should do>"}}
     ],
     "dismissed": [
       {{"signal": "<what looked anomalous>", "reason": "<why set aside>"}}
     ],
     "narration": "<2-4 self-contained sentences>",
     "cites": {{"narrative": "<name or empty>", "narrative_version": 0,
               "priors": ["<page group ids>"],
               "evidence_computed_at": "<from the bundle>"}},
     "generation": {{"consulted": [{{"source": "<tool or document>",
                                   "contribution": "<what it gave>"}}],
                    "problems": ["<errors, gaps, workarounds>"],
                    "unavailable": ["<what could not be obtained>"]}}
   }}

   The status vocabulary is exactly ok | attention | alarm — never
   "warning" or any other word. Empty lists are valid values.
2. The prose block — the bounded interpretation for humans, closing
   with your generation report.

The narration field must stand alone: campaign, date, verdict, and the
one or two things that matter.
"""

WEEKLY_DIRECTIVE = """\
Measure the week against the campaign narrative's stated goals: what
the narrative says this campaign should accomplish, what progressed,
what stalled, and whether the campaign's trajectory this week supports
its intent. Trend interpretation is where your judgment carries the
most value; a larger prose budget is available and should be spent
there.
"""


def render_template(kind, campaign, date):
    text = NIGHTLY_TEMPLATE.format(kind=kind, campaign=campaign, date=date,
                                   schema_version=SCHEMA_VERSION)
    if kind == 'weekly':
        text += '\n' + WEEKLY_DIRECTIVE
    return text


def system_prompt_text():
    """The corun SystemPrompt: the full template with per-run values
    deferred to the prompt content's bundle parameters."""
    return NIGHTLY_TEMPLATE.format(
        kind='nightly or weekly — read it from the bundle params',
        campaign='the campaign named in the bundle params',
        date='the date in the bundle params',
        schema_version=SCHEMA_VERSION,
    ) + '\nWHEN THE KIND IS weekly:\n' + WEEKLY_DIRECTIVE


def extract_artifact(page_content):
    """Extract the artifact JSON from the first fenced json block.

    Returns (artifact_dict_or_None, prose_text, problems). The prose is
    the content with the json block removed.
    """
    problems = []
    match = re.search(r'```json\s*(.*?)```', page_content or '',
                      re.DOTALL | re.IGNORECASE)
    if not match:
        return None, (page_content or '').strip(), ['no fenced json block found']
    try:
        artifact = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        return None, (page_content or '').strip(), [f'artifact json parse error: {e}']
    if not isinstance(artifact, dict):
        return None, (page_content or '').strip(), ['artifact json is not an object']
    prose = (page_content[:match.start()] + page_content[match.end():]).strip()
    return artifact, prose, problems


def validate_artifact(artifact):
    """Schema validation, hand-rolled and specific. Returns problems []."""
    problems = []

    def _req(key, types):
        value = artifact.get(key)
        if value is None:
            problems.append(f'missing required field: {key}')
            return None
        if not isinstance(value, types):
            problems.append(f'field {key} has wrong type')
            return None
        return value

    verdict = _req('verdict', str)
    if verdict is not None and verdict not in VERDICTS:
        problems.append(f'verdict {verdict!r} not in {VERDICTS}')

    axes = _req('axes', dict)
    if axes is not None:
        for axis in AXES:
            entry = axes.get(axis)
            if not isinstance(entry, dict):
                problems.append(f'axes.{axis} missing or not an object')
                continue
            if entry.get('status') not in VERDICTS:
                problems.append(f'axes.{axis}.status invalid')

    for key in ('key_metrics', 'top_issues', 'dismissed'):
        value = _req(key, list)
        if value is not None and any(not isinstance(x, dict) for x in value):
            problems.append(f'{key} entries must be objects')

    narration = _req('narration', str)
    if narration is not None and not narration.strip():
        problems.append('narration is empty')

    _req('cites', dict)
    _req('generation', dict)
    return problems


def verdict_at_least(verdict, floor):
    """True when verdict is at least as severe as floor."""
    try:
        return VERDICTS.index(verdict) >= VERDICTS.index(floor)
    except ValueError:
        return False


def slot(campaign, kind, date):
    """One artifact per (campaign, kind, date): the slot key."""
    return f'{campaign}/{kind}/{date}'
