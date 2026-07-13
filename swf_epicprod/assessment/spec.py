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

SCHEMA_VERSION = 3
VERDICTS = ('ok', 'attention', 'alarm')
AXES = ('arrivals', 'processing', 'failures', 'dispositions', 'infrastructure')

# corun-side names (bootstrap creates them; the trigger and enforcement
# reference them through the environment so a rename never hides here).
DEFAULT_SECTION = 'epicprod.assessment'
DEFAULT_BUNDLE_SECTION = 'epicprod.assessment.bundle'
DEFAULT_DEFINITION_NAME = 'campaign_assessment'
DEFAULT_SYSTEM_PROMPT_TITLE = 'epicprod campaign assessment template'

EXPERT_CONTEXT = """\
You are a senior ePIC production and operations expert supplying the judgment
layer for a report read by production coordinators, computing operations
experts, software experts, and physicists. You are their peer. Production-side
code renders the factual report directly from the evidence bundle. Your work is
to investigate material signals and return concise, structured interpretation:
operational meaning, relationships, issues, responsibilities, software impact,
and outlook. Do not rewrite the deterministic facts into a parallel narrative.

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
  synthesis, the JSON artifact, and a final contract check.

SET UP YOUR PROFESSIONAL CONTEXT BEFORE ASSESSING PRODUCTION:
- Call the TJAI get_profile tool and follow pagination until complete.
- Call get_ai_guidance with context="swf", location_name="swf-testbed", and
  audience="openai"; follow pagination until complete.
- Call get_entry_by_entry_id for "doc-writing-guidance".
- Apply that guidance throughout the investigation and report. If TJAI is
  unavailable, record the failure and resulting context limitation in the
  generation report; do not pretend the bootstrap succeeded.

PRODUCTION KNOWLEDGE:
The bundle and current campaign narrative are the primary run context. When a
material finding needs architectural, workflow, or data-model context, use the
GitHub read-only service to fetch the relevant document from these direct
locations. A summary is sufficient when it answers the question; this is a
reference set, not a checklist to consume on every run.
- https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/ARCHITECTURE_MAP.md
- https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/PCS.md
- https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_TASK_CATALOG.md
- https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_DATA_LINEAGE.md
- https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_ASSESSMENTS.md
- https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ACTION_STREAM.md
Do not describe these as TJAI-required documents. A failed fetch is material
only when the missing content prevents a production conclusion.

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
1. Read the complete bundle, its deterministic facts, narratives, manifest,
   and exact reporting interval before calling tools. Prior AI reports are not
   supplied and are not evidence.
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
6. Keep facts, interpretations, unresolved contradictions, and limitations
   distinct. A quiet interval and a legitimate null result are valid.
7. Bundle deltas come from production-owned analytics history, using the
   recorded campaign snapshot closest to one complete reporting window before
   the current state: one day for a daily, seven days for a weekly. The facts
   block carries the selected snapshot, its distance from the requested
   baseline, and the actual elapsed comparison interval.

If the submitted prompt contains a repair object, the prior output failed the
harness contract. Return a complete replacement artifact correcting every
listed validation problem and preserve only evidence-supported findings.

PROFESSIONAL PRESENTATION:
- Put the conclusion and operator significance first in ``assessment``.
- Use ePIC production vocabulary without expanding familiar acronyms.
- Name and link concrete campaigns, tasks, jobs, queues, datasets, releases,
  PRs, and source locations.
- Be concise for a quiet daily and comprehensive for a weekly. Depth follows
  operational substance and the reader's needs, not a fixed word target.
- Respect the reader's time sentence by sentence. Do not restate the campaign,
  report interval, verdict, or fact tables. Do not narrate source mechanics in
  place of findings, and compress lifecycle nulls to their operational meaning.
  This is a specific example of sloppy writing:

  "Attention. Campaign 26.06.0 advanced during the interval, 06:12 EDT 12 July
  through 06:12 EDT 13 July, through new JLab Rucio registrations rather than
  PanDA execution. The nightly action stream reported 599 new RECO files in the
  three 10×275 DIS NC Q² datasets. No 26.06.0 PanDA task began, completed,
  failed, or recovered."

  It spends 51 words repeating known framing and expanding a simple null.
  Convey the same information in 17 words:

  "JLab Rucio registered 599 RECO files across three 10×275 DIS NC Q²
  datasets; no PanDA tasks ran."

The ``generation`` object is an audit of this report's creation. State the
context and bundle material consulted; every MCP server/tool actually used and
what it contributed; and the failures, empty results, retries, evidence
conflicts, workarounds, or unavailable material that constrained this run. Do
not copy historical assessment-system errors from the action stream into this
object, and do not inventory standing absent metadata unless it materially
limited a conclusion in this report. If no live drill-down was warranted, say
why. Production-side code renders this as the final ``### Generation report``
section and links the full stored bundle.

"""

ARTIFACT_CONTRACT = """\
MACHINE-READABLE CONTRACT

Emit exactly one fenced json block with this shape and nothing else. The
production harness combines this bounded judgment with the bundle's
deterministic fact tables to produce the human report.

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
     "assessment": ["<one concise conclusion or operational implication>"],
     "activity_interpretation": ["<weekly relationship or trend; empty for daily when unnecessary>"],
     "software_findings": [
       {{"finding": "<version, PR, release, CI, or deployment finding>",
         "evidence": ["<links or exact tool references>"],
         "significance": "<production effect>"}}
     ],
     "top_issues": [
       {{"title": "<issue>", "severity": "<attention|alarm>",
         "evidence": ["<refs>"], "action": "<human action>",
         "owner": "<owner>"}}
     ],
     "dismissed": [
       {{"signal": "<examined signal>", "reason": "<why it was set aside>"}}
     ],
     "outlook": ["<evidence-grounded weekly expectation or decision>"],
     "narration": "<2-4 self-contained sentences>",
     "cites": {{
       "narrative": "<name or empty>",
       "narrative_version": 0,
       "evidence_computed_at": "<from bundle>",
       "bundle_id": "<bundle artifact id>"
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
Use ``assessment`` for one to five high-information conclusions, not a summary
of the fact tables. The narration is metadata for compact delivery channels.

"""

DAILY_TEMPLATE = """\
DAILY ASSIGNMENT

Supply the expert judgment for the ePIC Production Campaign {campaign} daily
production report for {date}. Production code will render the exact facts,
comparisons, evidence labels, full-bundle link, issues table, and generation
report. Your artifact should let a production coordinator understand:

- what productive work occurred in the reporting interval;
- what began, completed, failed, recovered, or materially changed;
- whether the campaign advanced toward its stated purpose;
- what requires human attention now; and
- whether any current condition requires action despite a quiet interval.

The exact interval and its evidence timestamps are in the bundle. Interpret
and present time in America/New_York. The campaign's lifetime state supplies
context, but the daily's subject is activity and change in this interval.

Use the supplied deterministic ``facts`` block as the report's factual record.
Use the raw rollup, deltas, narratives, and manifest to understand and qualify
those facts. Investigate only material signals using the services described
above. Do not manufacture concern or novelty when the interval was quiet.

The mechanical verdict floor is the minimum permissible verdict. Raise it only
when the investigated evidence warrants doing so.

Return only the contract JSON. Put the conclusion-first interpretation in
``assessment`` and actionable concerns in ``top_issues``. Daily
``activity_interpretation``, ``software_findings``, and ``outlook`` may be empty
when the interval supplies no material content for them.

""" + ARTIFACT_CONTRACT

WEEKLY_TEMPLATE = """\
WEEKLY ASSIGNMENT

Supply the expert judgment for the standalone ePIC Production Campaign
{campaign} weekly summary for {date}. Production code renders the exact weekly
and current-state facts. Your artifact explains their operational meaning,
relationships, constraints, software/release readiness, responsibilities, and
near-term outlook.

The bundle supplies production-owned weekly activity and state history,
campaign and general narratives, deterministic facts, the raw rollup, deltas,
and the source manifest. Prior AI reports are deliberately absent. Re-establish
material current state through the live services described above.

The weekly subject is the complete seven-day evidence window, not the previous
24 hours and not the campaign's lifetime totals. Use lifetime state only as
context. Use the seven-day production snapshot comparison when available; if
the selected snapshot is materially displaced from seven days, state the
actual interval and do not describe its delta as weekly. No production metric
depends on a prior AI report. On the first weekly, simply omit report-to-report
claims such as verdict movement, issue carry-over, or forecast accuracy.

Assess the week as a production expert rather than concatenating daily
summaries. Reconcile repeated observations, distinguish transient incidents
from standing limitations, and explain how execution, data management,
software, sites, and campaign intent fit together. A quiet week remains a
complete report; it does not require artificial novelty.

Use changed production as the starting point for drill-down. When the bundle
identifies datasets or locations with arrivals, transitions, failures, or
disposition changes, investigate those changed objects before any
representative sample. Never substitute an unrelated healthy dataset for the
health of a changed dataset. If older sweep records lack dataset-level detail,
state exactly which part of the week is unresolved and use the complete detail
that is available; do not infer that the unobserved locations were healthy.

Establish software and release state in proportion to the campaign: identify
the versions actually used, changes merged or released during the week, and
whether those changes reached production. A missing PCS tag is material only
if the live production identity cannot be established from PanDA, queue,
container, release, GitHub, or LXR evidence.

The mechanical verdict floor is the minimum permissible verdict. Raise it only
when the investigated evidence warrants doing so.

Return only the contract JSON. Use ``assessment`` for the executive judgment,
``activity_interpretation`` for relationships and trends not stated by the fact
tables, ``software_findings`` for verified software state,
``top_issues`` for responsibilities, and ``outlook`` for evidence-grounded
expectations and decisions.

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
        'schema_version', 'verdict', 'axes', 'assessment',
        'activity_interpretation', 'software_findings', 'top_issues',
        'dismissed', 'outlook', 'narration', 'cites', 'generation',
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

    for key in ('assessment', 'activity_interpretation', 'outlook'):
        value = _req(key, list)
        if value is not None and any(
                not isinstance(x, str) or not x.strip() for x in value):
            problems.append(f'{key} entries must be non-empty strings')
    if len(artifact.get('assessment') or []) > 5:
        problems.append('assessment must contain at most five conclusions')

    for key in ('software_findings', 'top_issues', 'dismissed'):
        value = _req(key, list)
        if value is not None and any(not isinstance(x, dict) for x in value):
            problems.append(f'{key} entries must be objects')

    required_item_keys = {
        'software_findings': {'finding', 'evidence', 'significance'},
        'top_issues': {'title', 'severity', 'evidence', 'action', 'owner'},
        'dismissed': {'signal', 'reason'},
    }
    for field, keys in required_item_keys.items():
        for index, item in enumerate(artifact.get(field) or []):
            if isinstance(item, dict) and set(item) != keys:
                problems.append(
                    f'{field}[{index}] keys must be exactly {sorted(keys)}')

    for field in ('software_findings', 'top_issues'):
        for index, item in enumerate(artifact.get(field) or []):
            if isinstance(item, dict) and not isinstance(item.get('evidence'), list):
                problems.append(f'{field}[{index}].evidence must be a list')
    for index, item in enumerate(artifact.get('top_issues') or []):
        if (isinstance(item, dict)
                and item.get('severity') not in ('attention', 'alarm')):
            problems.append(f'top_issues[{index}].severity invalid')

    narration = _req('narration', str)
    if narration is not None and not narration.strip():
        problems.append('narration is empty')

    cites = _req('cites', dict)
    if cites is not None:
        cite_keys = {'narrative', 'narrative_version',
                     'evidence_computed_at', 'bundle_id'}
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


def validate_remainder(remainder):
    """The model contract is one JSON block; report prose is harness-owned."""
    if (remainder or '').strip():
        return ['output must contain only the fenced json artifact']
    return []


def validate_bundle_citations(artifact, bundle):
    """Verify that the judgment identifies the exact evidence it received."""
    problems = []
    cites = artifact.get('cites') or {}
    bundle_artifact = bundle.get('artifact') or {}
    rollup = bundle.get('rollup') or {}
    narrative = (bundle.get('narratives') or {}).get('campaign') or {}
    expected = {
        'bundle_id': str(bundle_artifact.get('id') or ''),
        'evidence_computed_at': str(rollup.get('generated_at') or ''),
        'narrative': str(narrative.get('name') or ''),
        'narrative_version': int(narrative.get('version') or 0),
    }
    for key, value in expected.items():
        if cites.get(key) != value:
            problems.append(
                f'cites.{key} does not identify the submitted bundle evidence')
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
