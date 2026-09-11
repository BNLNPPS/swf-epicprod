"""The segfault diagnosis contract: the corun section and definition, the
system prompt, and the artifact schema (docs/SEGFAULT_DIAGNOSIS.md,
Diagnosis). The harness pattern is the campaign assessment's
(EPICPROD_ASSESSMENTS_V1.md): deterministic evidence in, one fenced JSON
artifact out, validated here, one bounded repair run, quarantine on a
second failure.
"""
import json
import re

SCHEMA_VERSION = 1
DEFAULT_SECTION = 'epicprod.segfault'
DEFAULT_BUNDLE_SECTION = 'epicprod.segfault.bundle'
DEFINITION_NAME = 'segfault_diagnosis'
SYSTEM_PROMPT_TITLE = 'epicprod segfault diagnosis template'
WORKER_TIMEOUT_S = 30 * 60
BUDGET_S = 15 * 60

CLASSIFICATIONS = ('software_defect', 'configuration', 'event_shaped', 'platform', 'unresolved')
OPERATOR_ACTIONS = ('bump_image', 'change_configuration', 'hold_configuration',
                    'hand_off', 'accept_loss', 'none')
CONFIDENCES = ('confirmed', 'supported', 'unresolved')

SYSTEM_PROMPT = """\
You are a senior ePIC software and production expert diagnosing one payload
crash signature for production operators and the software experts who will
fix it. The bundle in the prompt content is the evidence: the signature
record (class, crash counts, rate, time to death, queues, hosts), the
configuration (campaign, PCS task, container image the task ran, detector
version, payload version), the crash trace as read from a representative
job's payload output (program, stage, crashing frame, the frames, the lines
before the crash), the representative and reproduction job records, and the
reproduction package README when one exists. Production code records the
facts; your work is the judgment.

EXECUTION BUDGET — COMPLETE THE DIAGNOSIS WITHIN 15 MINUTES OF WALL TIME:
- The worker terminates the run at thirty minutes and a terminated run
  delivers nothing. The submission carries submitted_at (UTC); tool results
  carry current timestamps; compare them to know the budget spent.
- Read the bundle first. Use the tools for what the bundle cannot answer:
  the source behind the crashing frame, and whether the crash is a known
  issue. Keep tool calls bounded; one retry on a slow or failing service,
  then record the limitation and move on.
- Stop investigating by the tenth minute and spend the rest on the
  artifact and its contract check.

INVESTIGATION ROUTES:
- LXR (the EIC code index: EICrecon, epic, npsim, DD4hep, jana2, edm4eic,
  podio and more): lxr_ident on the crashing function and the frames beneath
  it; lxr_source to read the code at the frame; lxr_search for the error
  text in the lines before the crash.
- GitHub (read-only): issues and pull requests in eic/EICrecon, eic/epic,
  eic/npsim, AIDASoft/DD4hep, JeffersonLab/JANA2 and AIDASoft/podio that
  match the crashing frame, the exception text or the volume; compare the
  campaign image's software versions (in the frames' library paths) with
  the fix's release.
- SWF Testbed: panda_segfault_signature for the full signature and its
  jobs, panda_study_job for a job's record and diagnosis, panda_segfault_catalog
  for other signatures with the same frame.

THE TASK. Name the crashing component and function. State whether the
frame matches a known issue, with the issue or pull request URL and the
release that fixes it when one exists. Classify the crash as one of:
software_defect (a bug in the software stack), configuration (a geometry,
beam or steering setting the software does not support), event_shaped (a
class of input the software mishandles; typically sparse, varying time to
death), platform (memory, node or site condition; typically reproduced at
one site only). State what a production operator should do: bump_image
(name the image or release), change_configuration (name the change),
hold_configuration, hand_off (to the software experts, with the handoff
text), accept_loss, or none. Write the handoff text when handing off: the
image, the row, the command, the trace, how often and where it happens,
and the reproduction outcome, as the expert package README states them.

VERDICT FLOOR. A signature the record classes as configuration_dead cannot
be classified below configuration (it is configuration or software_defect).
A signature whose reproduction outcome is reproduced cannot be classified
platform. A classification of confirmed confidence needs a live tool result
in the investigation record that supports it; otherwise it is supported or
unresolved.

OUTPUT. Exactly one fenced json block and nothing else, this shape,
schema_version {schema_version}:

```json
{{
  "schema_version": {schema_version},
  "component": "<library or package, e.g. eicrecon, podio, npsim/DD4hep>",
  "function": "<the crashing function as the trace names it>",
  "stage": "simulation | reconstruction | unknown",
  "classification": "software_defect | configuration | event_shaped | platform | unresolved",
  "confidence": "confirmed | supported | unresolved",
  "known_issue": {{"url": "<issue or PR URL or empty>", "fixed_in": "<release, tag or image, or empty>", "note": "<one sentence>"}},
  "operator_action": "bump_image | change_configuration | hold_configuration | hand_off | accept_loss | none",
  "action_detail": "<the image, the configuration change, or why none>",
  "diagnosis": ["<two to five sentences of the reading, each self-contained>"],
  "handoff_text": "<the expert handoff when operator_action is hand_off, else empty>",
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


def extract_artifact(page_content):
    """The artifact JSON from the first fenced json block: (artifact or
    None, remainder text, problems)."""
    problems = []
    match = re.search(r'```json\s*(.*?)```', page_content or '', re.DOTALL | re.IGNORECASE)
    if not match:
        return None, (page_content or '').strip(), ['no fenced json block found']
    try:
        artifact = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        return None, (page_content or '').strip(), [f'artifact json parse error: {e}']
    if not isinstance(artifact, dict):
        return None, (page_content or '').strip(), ['artifact json is not an object']
    remainder = (page_content[:match.start()] + page_content[match.end():]).strip()
    return artifact, remainder, problems


def validate_artifact(artifact, bundle=None):
    """The schema and the verdict floor; returns the list of problems."""
    problems = []
    if artifact.get('schema_version') != SCHEMA_VERSION:
        problems.append(f'schema_version must be {SCHEMA_VERSION}')
    for key in ('component', 'function', 'stage', 'classification', 'confidence',
                'operator_action', 'action_detail', 'handoff_text'):
        if not isinstance(artifact.get(key), str):
            problems.append(f'{key} must be a string')
    if artifact.get('stage') not in ('simulation', 'reconstruction', 'unknown'):
        problems.append('stage must be simulation, reconstruction or unknown')
    if artifact.get('classification') not in CLASSIFICATIONS:
        problems.append(f'classification must be one of {", ".join(CLASSIFICATIONS)}')
    if artifact.get('confidence') not in CONFIDENCES:
        problems.append(f'confidence must be one of {", ".join(CONFIDENCES)}')
    if artifact.get('operator_action') not in OPERATOR_ACTIONS:
        problems.append(f'operator_action must be one of {", ".join(OPERATOR_ACTIONS)}')
    known = artifact.get('known_issue')
    if not isinstance(known, dict) or not all(isinstance(known.get(k), str) for k in ('url', 'fixed_in', 'note')):
        problems.append('known_issue must be an object with url, fixed_in and note strings')
    diagnosis = artifact.get('diagnosis')
    if not isinstance(diagnosis, list) or not diagnosis or not all(isinstance(d, str) and d.strip() for d in diagnosis):
        problems.append('diagnosis must be a non-empty list of sentences')
    elif len(diagnosis) > 8:
        problems.append('diagnosis must hold at most eight sentences')
    if artifact.get('operator_action') == 'hand_off' and not (artifact.get('handoff_text') or '').strip():
        problems.append('handoff_text is required when operator_action is hand_off')
    gen = artifact.get('generation_report')
    if not isinstance(gen, dict):
        problems.append('generation_report must be an object')
    else:
        for key in ('consulted', 'investigation', 'problems', 'unavailable'):
            if not isinstance(gen.get(key), list):
                problems.append(f'generation_report.{key} must be a list')
        if artifact.get('confidence') == 'confirmed' and not gen.get('investigation'):
            problems.append('a confirmed classification needs at least one investigation record')
    # The verdict floor from the record.
    sig = (bundle or {}).get('signature') or {}
    if sig.get('class') == 'configuration_dead' and artifact.get('classification') in ('event_shaped', 'platform', 'unresolved'):
        problems.append('a configuration_dead signature is classified configuration or software_defect')
    if sig.get('reproduction_outcome') == 'reproduced' and artifact.get('classification') == 'platform':
        problems.append('a reproduced signature is not classified platform')
    return problems


def validate_remainder(remainder):
    """The model emits the artifact and nothing else."""
    text = (remainder or '').strip()
    return ['the response carries text outside the json artifact'] if len(text) > 200 else []


def render_report(bundle, artifact):
    """The human report registered as the assessment: the facts from the
    bundle, the judgment from the artifact, the generation report last."""
    sig = bundle.get('signature') or {}
    trace = bundle.get('trace') or {}
    known = artifact.get('known_issue') or {}
    lines = [
        f"# Segfault diagnosis: {sig.get('key', '')}",
        '',
        ' '.join(artifact.get('diagnosis') or []),
        '',
        '## The signature',
        '',
        f"- Class {sig.get('class', '')}, {sig.get('crashes', '')} crashed jobs, rate "
        f"{sig.get('rate_pct', '')}, time to death p10/p50 {sig.get('minutes_p10', '')}/"
        f"{sig.get('minutes_p50', '')} min, {len(sig.get('site_names') or [])} queue(s), "
        f"{sig.get('hosts', '')} hosts",
        f"- Configuration: {(sig.get('configuration') or {}).get('prod_task', '')}; image "
        f"{(sig.get('configuration') or {}).get('container_image_ran') or (sig.get('configuration') or {}).get('container_image', '')}",
        f"- Trace: {trace.get('program', '')} {trace.get('stage', '')}, crashing frame "
        f"{trace.get('frame', '')}{' in ' + trace['library'] if trace.get('library') else ''} "
        f"(status {trace.get('trace_status', 'unknown')}, from job {trace.get('source_pandaid', '')})",
        f"- Reproduction: {sig.get('reproduction_outcome') or 'none yet'}",
        '',
        '## The diagnosis',
        '',
        f"- Component: {artifact.get('component', '')}; function: {artifact.get('function', '')}; "
        f"stage: {artifact.get('stage', '')}",
        f"- Classification: {artifact.get('classification', '')} ({artifact.get('confidence', '')})",
        f"- Known issue: {known.get('url') or 'none found'}"
        f"{'; fixed in ' + known['fixed_in'] if known.get('fixed_in') else ''}"
        f"{'; ' + known['note'] if known.get('note') else ''}",
        f"- Operator action: {artifact.get('operator_action', '')}: {artifact.get('action_detail', '')}",
    ]
    if (artifact.get('handoff_text') or '').strip():
        lines += ['', '## Handoff', '', artifact['handoff_text'].strip()]
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
