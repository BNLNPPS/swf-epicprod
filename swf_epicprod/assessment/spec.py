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

SCHEMA_VERSION = 2
VERDICTS = ('ok', 'attention', 'alarm')
AXES = ('arrivals', 'processing', 'failures', 'dispositions', 'infrastructure')

# corun-side names (bootstrap creates them; the trigger and enforcement
# reference them through the environment so a rename never hides here).
DEFAULT_SECTION = 'epicprod.assessment'
DEFAULT_DEFINITION_NAME = 'campaign_assessment'
DEFAULT_SYSTEM_PROMPT_TITLE = 'epicprod campaign assessment template'

EXPERT_CONTEXT = """\
You are a senior ePIC production and operations expert preparing an
evidence-grounded report for production coordinators, computing operations
experts, software experts, and physicists. You are their peer. Exercise
editorial and operational judgment: identify what matters, investigate it,
reconcile the evidence, and write a report that rewards the time experts spend
reading it. Do not write a work log, a tutorial, or a schema-shaped data dump.

EXECUTION BUDGET — COMPLETE THE REPORT WITHIN 10 MINUTES OF WALL TIME:
- Treat ten minutes from the start of the run as the maximum available time,
  not as an investigation target. Calibrate the breadth and depth of your work
  so that a complete final response is submitted inside that bound.
- Read the supplied bundle first and use it to select only the live checks that
  can materially change the assessment. Do not exhaustively enumerate healthy
  systems, catalogs, repositories, tasks, or files.
- Keep tool calls focused and bounded. If a service is slow, unavailable, or
  inconclusive after a reasonable retry, record the limitation and move on.
- Stop live investigation early enough to reserve substantial time for
  synthesis, the JSON artifact, the professional prose report, and a final
  contract check. A complete evidence-bounded report delivered on time is more
  valuable than an unfinished exhaustive investigation.

SET UP YOUR PROFESSIONAL CONTEXT BEFORE ASSESSING PRODUCTION:
- Call the TJAI get_profile tool and follow pagination until complete.
- Call get_ai_guidance with context="swf", location_name="swf-testbed", and
  audience="openai"; follow pagination until complete.
- Call get_entry_by_entry_id for "doc-writing-guidance".
- Apply that guidance throughout the investigation and report. If TJAI is
  unavailable, record the failure and resulting context limitation in the
  generation report; do not pretend the bootstrap succeeded.

YOUR READ-ONLY INVESTIGATION SERVICES:

SWF TESTBED — the primary ePIC production surface.
- epicprod_campaign_status: campaign rollup and mechanical verdict floor.
- epicprod_list_actions: what production automation actually did. Prefer
  summarize=true for a window, then drill into failed or material actions.
- panda_get_activity: aggregate PanDA activity.
- panda_list_tasks: task state and transitions.
- panda_error_summary and panda_diagnose_jobs: failure patterns.
- panda_study_job: a representative job and its real log evidence.
- panda_resource_usage: site and core-hour accounting.
- panda_get_queue and panda_harvester_workers: queue and worker state.
- pcs_prodtask_get and pcs_dataset_get: canonical production identity,
  configuration, release information, and Rucio DID.
Start broad, then drill down only where the bundle contains activity, change,
contradiction, or a standing issue whose claimed fix needs verification.

BNL RUCIO — the bnl_rucio_* tools on SWF Testbed expose the PanDA production
output and log catalog. Resolve the canonical DID through PCS when possible.
Otherwise call bnl_rucio_list_dids in scope "group.EIC" with a campaign
wildcard and account for block suffixes such as .b1 and .b2. Inspect metadata,
content, files, replication rules, locks, replicas, and RSE state. One empty
query never proves absence: inspect scopes and retry with the correct identity.

JLAB RUCIO — the jlab_rucio_* tools on SWF Testbed expose science data managed
at JLab. Call jlab_rucio_list_dids in scope "epic" first with a campaign
wildcard and type DATASET; campaign DIDs commonly have path-like names under
/RECO or /SIMU. These two Rucio instances are separate catalogs, not counters
that must agree. Replication rules are authoritative for managed placement;
PFNs and replica listings are access evidence and may include transient
staging endpoints.

XROOTD — physical JLab storage access. Use it after Rucio identifies the
expected data, or when catalog, sweep, and physical-file evidence conflict.
An XRootD permission failure does not prove that a Rucio DID is absent.

SOFTWARE — software state is production state.
- Use GitHub read-only tools for repository releases and tags, PR status,
  reviews, merge state, issues, and Actions/CI.
- Use LXR to locate the implementation and establish what a software version
  can actually do or where an observed error path originates.
- Establish the release, container, geometry, simulation, and reconstruction
  versions actually used by PCS, the PanDA task, and the queue before drawing
  a software conclusion.
- Distinguish open, approved, merged, released, included in a container, and
  actually deployed in production. A merged fix is not a deployed fix.
Use software investigation when a change affected the window, software may
explain a production result, or release readiness matters to the weekly.

All services are for assessment and are READ-ONLY in this job. Never call a
mutation, proposal, decision, registration, submission, messaging, or workflow
control tool. Never change production or external systems.

EVIDENCE AND INVESTIGATION DISCIPLINE:
1. Read the complete bundle, narratives, prior reports, manifest, and exact
   reporting interval before calling tools.
2. Identify the few material activities, changes, claimed fixes, and
   contradictions that require live investigation.
3. Use the action stream, then PanDA and PCS, then Rucio, storage, and software
   services as the evidence requires. Chain calls until the operational claim
   is supported or the gap is explicit.
4. Treat PCS intent, PanDA execution, BNL Rucio registration, JLab Rucio data
   management, XRootD accessibility, and GitHub/LXR software state as distinct
   evidence layers. Never silently choose one when they disagree.
5. Every number and concrete claim must trace to the bundle or a tool result
   from this run. Do not estimate, interpolate, or invent precision.
6. Report facts, interpretations, unresolved contradictions, and limitations
   distinctly. A quiet interval and a legitimate null result are valid.

If the submitted prompt contains a repair object, the prior output failed the
harness contract. Produce a complete replacement report, correct every listed
validation problem, preserve only evidence-supported findings, and mention the
repair once in the final Generation report.

PROFESSIONAL PRESENTATION:
- Lead with the conclusion and operator significance.
- Use ePIC production vocabulary without expanding familiar acronyms.
- Name and link concrete campaigns, tasks, jobs, queues, datasets, releases,
  PRs, and source locations.
- Select structure and level of detail through professional editorial judgment.
- Be concise for a quiet daily and comprehensive for a weekly. Depth follows
  operational substance and the reader's needs, not a fixed word target.
- Respect the reader's time sentence by sentence. Do not restate the campaign,
  report interval, or verdict when the title and artifact already establish
  them. Do not narrate source mechanics in place of findings, and compress
  lifecycle nulls to their operational meaning. This is a specific example of
  sloppy writing:

  "Attention. Campaign 26.06.0 advanced during the interval, 06:12 EDT 12 July
  through 06:12 EDT 13 July, through new JLab Rucio registrations rather than
  PanDA execution. The nightly action stream reported 599 new RECO files in the
  three 10×275 DIS NC Q² datasets. No 26.06.0 PanDA task began, completed,
  failed, or recovered."

  It spends 51 words repeating known framing and expanding a simple null.
  Convey the same information in 17 words:

  "JLab Rucio registered 599 RECO files across three 10×275 DIS NC Q²
  datasets; no PanDA tasks ran."

The final prose section MUST be "### Generation report". It must state the
context and bundle material consulted; every MCP server/tool used and what it
contributed; failures, empty results, retries, evidence conflicts, and
workarounds; anything unavailable; and the effect on confidence. If no live
drill-down was warranted, say why. Nothing may follow this section.

"""

ARTIFACT_CONTRACT = """\
MACHINE-READABLE CONTRACT

Before the prose report, emit one fenced json block with exactly this shape.
This artifact supports indexing, continuity, and deterministic enforcement; it
is not the report and must not dictate the report's prose.

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
       {{"name": "<metric>", "value": "<value from evidence>",
         "delta": "<comparison from evidence>", "ref": "<source>"}}
     ],
     "top_issues": [
       {{"title": "<issue>", "severity": "<attention|alarm>",
         "evidence": ["<refs>"], "action": "<human action>",
         "owner": "<owner>"}}
     ],
     "standing_issues": [
       {{"title": "<issue>",
         "status": "<new|unchanged|improved|worsened|resolved>",
         "since": "<first-seen date>", "note": "<current shape>"}}
     ],
     "dismissed": [
       {{"signal": "<examined signal>", "reason": "<why it was set aside>"}}
     ],
     "narration": "<2-4 self-contained sentences>",
     "cites": {{
       "narrative": "<name or empty>",
       "narrative_version": 0,
       "priors": ["<page group ids>"],
       "evidence_computed_at": "<from bundle>"
     }},
     "generation": {{
       "consulted": [
         {{"source": "<tool or document>",
           "contribution": "<what it established>"}}
       ],
       "problems": ["<errors, conflicts, retries, workarounds>"],
       "unavailable": ["<what could not be obtained>"]
     }}
   }}

Use only the defined status vocabularies and fields. Empty lists are legitimate.
The narration is metadata for compact consumers; do not append it to the prose.

"""

DAILY_TEMPLATE = """\
DAILY ASSIGNMENT

Prepare the ePIC Production Campaign {campaign} daily production report for
{date}. The daily is an operational delta report for expert peers. It should
let a production coordinator understand, quickly and accurately:

- what productive work occurred in the reporting interval;
- what began, completed, failed, recovered, or materially changed;
- whether the campaign advanced toward its stated purpose;
- what requires human attention now; and
- how the concerns carried from the preceding daily have evolved.

The exact interval and its evidence timestamps are in the bundle. Interpret
and present time in America/New_York. The campaign's lifetime state supplies
context, but the daily's subject is activity and change in this interval.

Use the supplied narratives, prior daily reports, rollup, window activity,
deltas, and manifest as the starting record. Investigate the signals that
matter using the services described above. Verify claimed fixes and apparent
contradictions. Do not manufacture concern or novelty when the interval was
quiet, and do not allow a quiet interval to erase an important open production
condition.

The mechanical verdict floor is the minimum permissible verdict. Raise it only
when the investigated evidence warrants doing so.

PROSE REPORT

After the JSON artifact, write a polished markdown report with:

# ePIC Production Campaign {campaign} — Daily Report, {date}

### Operational assessment
A conclusion-first account of the interval and its significance.

### Production activity
The production accomplished or attempted: data products, processing, sites,
task transitions, campaign advancement, and material software/release activity.

### Issues and follow-up
New or changed problems, decisions or actions required, and the current state
of concerns inherited from the preceding daily. Identify owners and link the
objects that require attention.

### Generation report
The mandatory final provenance and limitations account defined above.

Choose tables, bullets, or prose according to the evidence and the reader's
needs. Link every material campaign, task, job, queue, dataset, release, PR,
and source reference.

""" + ARTIFACT_CONTRACT

WEEKLY_TEMPLATE = """\
WEEKLY ASSIGNMENT

Prepare the standalone ePIC Production Campaign {campaign} weekly production
summary for {date}. A reader should need no previous report to understand the
campaign's purpose, present state, production accomplished during the week,
material constraints, software and release readiness, responsibilities, and
near-term outlook.

The bundle supplies the completed week's daily reports, the preceding weekly,
campaign and general narratives, the weekly rollup and activity, deltas, and
the source manifest. Read the daily reports as the operational record of the
week, then re-establish the current state through the live services described
above. Interpret and present time in America/New_York.

Assess the week as a production expert rather than concatenating daily
summaries. Reconcile repeated observations, distinguish transient incidents
from standing limitations, and explain how execution, data management,
software, sites, and campaign intent fit together. A quiet week remains a
complete report; it does not require artificial novelty.

The mechanical verdict floor is the minimum permissible verdict. Raise it only
when the investigated evidence warrants doing so.

PROSE REPORT

After the JSON artifact, write a polished markdown report with:

# ePIC Production Campaign {campaign} — Weekly Summary, {date}

### Executive assessment
The campaign status and the week's operational meaning, including the most
important actions or decisions.

### Campaign state
Purpose, scope, accumulated production, completion, dispositions, processing
state, and the resource picture needed to understand the campaign now.

### Production this week
What the week produced and advanced, task and job outcomes, site performance,
data registration and movement, and changes relative to the preceding week.

### Software and release state
The versions actually used, relevant PR/release/CI developments, and whether
software capability or deployment state enabled or constrained production.

### Issues and responsibilities
The standing and newly material issues, their trajectory, evidence, owner, and
required action.

### Outlook
Expected production and decisions for the coming week, grounded in campaign
intent and current readiness.

### Generation report
The mandatory final provenance and limitations account defined above.

Choose tables, bullets, or prose according to the evidence and the reader's
needs. Link every material campaign, task, job, queue, dataset, release, PR,
and source reference.

""" + ARTIFACT_CONTRACT

TEMPLATES = {
    'daily': EXPERT_CONTEXT + DAILY_TEMPLATE,
    'weekly': EXPERT_CONTEXT + WEEKLY_TEMPLATE,
}


def render_template(kind, campaign, date):
    return TEMPLATES[kind].format(campaign=campaign, date=date,
                                  schema_version=SCHEMA_VERSION)


def system_prompt_text(kind):
    """The corun SystemPrompt for one kind: the full template with
    per-run values deferred to the prompt content's bundle parameters."""
    return TEMPLATES[kind].format(
        campaign='the campaign named in the bundle params',
        date='the date in the bundle params',
        schema_version=SCHEMA_VERSION,
    )


def definition_name(kind):
    return f'{DEFAULT_DEFINITION_NAME}_{kind}'


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

    expected_keys = {
        'schema_version', 'verdict', 'axes', 'key_metrics', 'top_issues',
        'standing_issues', 'dismissed', 'narration', 'cites', 'generation',
    }
    missing_keys = expected_keys - set(artifact)
    extra_keys = set(artifact) - expected_keys
    for key in sorted(missing_keys):
        problems.append(f'missing required field: {key}')
    for key in sorted(extra_keys):
        problems.append(f'unexpected top-level field: {key}')

    def _req(key, types):
        value = artifact.get(key)
        if value is None:
            problems.append(f'missing required field: {key}')
            return None
        if not isinstance(value, types):
            problems.append(f'field {key} has wrong type')
            return None
        return value

    schema_version = _req('schema_version', int)
    if schema_version is not None and schema_version != SCHEMA_VERSION:
        problems.append(
            f'schema_version {schema_version!r} != {SCHEMA_VERSION}')

    verdict = _req('verdict', str)
    if verdict is not None and verdict not in VERDICTS:
        problems.append(f'verdict {verdict!r} not in {VERDICTS}')

    axes = _req('axes', dict)
    if axes is not None:
        if set(axes) != set(AXES):
            problems.append(
                f'axes keys must be exactly {AXES}; got {tuple(axes)}')
        for axis in AXES:
            entry = axes.get(axis)
            if not isinstance(entry, dict):
                problems.append(f'axes.{axis} missing or not an object')
                continue
            if set(entry) != {'status', 'note'}:
                problems.append(
                    f'axes.{axis} keys must be exactly status, note')
            if entry.get('status') not in VERDICTS:
                problems.append(f'axes.{axis}.status invalid')

    for key in ('key_metrics', 'top_issues', 'standing_issues', 'dismissed'):
        value = _req(key, list)
        if value is not None and any(not isinstance(x, dict) for x in value):
            problems.append(f'{key} entries must be objects')

    required_item_keys = {
        'key_metrics': {'name', 'value', 'delta', 'ref'},
        'top_issues': {'title', 'severity', 'evidence', 'action', 'owner'},
        'standing_issues': {'title', 'status', 'since', 'note'},
        'dismissed': {'signal', 'reason'},
    }
    for field, keys in required_item_keys.items():
        for index, item in enumerate(artifact.get(field) or []):
            if isinstance(item, dict) and set(item) != keys:
                problems.append(
                    f'{field}[{index}] keys must be exactly {sorted(keys)}')

    ledger_states = ('new', 'unchanged', 'improved', 'worsened', 'resolved')
    for item in artifact.get('standing_issues') or []:
        if isinstance(item, dict) and item.get('status') not in ledger_states:
            problems.append(
                f"standing_issues status {item.get('status')!r} not in "
                f'{ledger_states}')

    narration = _req('narration', str)
    if narration is not None and not narration.strip():
        problems.append('narration is empty')

    cites = _req('cites', dict)
    if cites is not None:
        cite_keys = {'narrative', 'narrative_version', 'priors',
                     'evidence_computed_at'}
        if set(cites) != cite_keys:
            problems.append('cites keys do not match the required schema')

    generation = _req('generation', dict)
    if generation is not None:
        generation_keys = {'consulted', 'problems', 'unavailable'}
        if set(generation) != generation_keys:
            problems.append(
                'generation keys must be exactly consulted, problems, unavailable')
        consulted = generation.get('consulted')
        if not isinstance(consulted, list) or not consulted:
            problems.append('generation.consulted must be a non-empty list')
        else:
            for index, item in enumerate(consulted):
                if (not isinstance(item, dict)
                        or set(item) != {'source', 'contribution'}
                        or not str(item.get('source') or '').strip()
                        or not str(item.get('contribution') or '').strip()):
                    problems.append(
                        f'generation.consulted[{index}] is incomplete')
        for key in ('problems', 'unavailable'):
            if not isinstance(generation.get(key), list):
                problems.append(f'generation.{key} must be a list')
    return problems


def validate_prose(prose, kind):
    """Validate the human report's structural reading contract."""
    problems = []
    headings = re.findall(r'^###\s+(.+?)\s*$', prose or '', re.MULTILINE)
    required = (
        ('Executive assessment', 'Campaign state', 'Production this week',
         'Software and release state', 'Issues and responsibilities', 'Outlook',
         'Generation report')
        if kind == 'weekly'
        else ('Operational assessment', 'Production activity',
              'Issues and follow-up',
              'Generation report')
    )
    for heading in required:
        if heading not in headings:
            problems.append(f'prose is missing required section: ### {heading}')
    if not headings or headings[-1] != 'Generation report':
        problems.append('### Generation report must be the final H3 section')
        return problems

    match = re.search(
        r'^###\s+Generation report\s*$([\s\S]*)\Z',
        prose or '', re.MULTILINE)
    body = (match.group(1) if match else '').strip()
    if len(body) < 80:
        problems.append('### Generation report is missing substantive provenance')
    if re.search(r'(?im)^\s*(?:---\s*)?\*\*Narration:', body):
        problems.append('narration must remain metadata, not follow the report')
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
