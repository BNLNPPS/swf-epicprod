# ePIC Production Validation

ePIC production validation connects three systems. epicprod runs automated ePIC
production through PanDA, producing simulation and reconstruction data. Hydra,
the ePIC validation application, is the evaluator: it runs validation benchmarks
over the produced data. epicprod uses Hydra's validation to mark produced data
as validated. Data that fails validation means the delivered event count
decreases, and automated production continues to restore the count. Validation
outcomes can also serve LLM assessments; argus-ai will be one mechanism for a
natural-language judgment.

This document describes the epicprod → validation → epicprod loop and the two
interfaces that join the systems: the availability signal from epicprod to
Hydra, and the validation results return from Hydra to epicprod and other
consumers.

This plan was discussed in the
[July 28 2026 prod/validation meeting](https://docs.google.com/document/d/1JA8GIQae30Ru62kgDN2pzqK90XBbQKz4LffXYbWNgIY/edit?tab=t.0).
Progress will be followed up biweekly.

The LLM assessment application is described in
[argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).
The loop draws on the produced-data availability signal in
[EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md), the configuration record in
[PCS.md](PCS.md), and the current effort to answer the data sample completion
question in the production system: events delivered, and event count target, for
each produced sample.

## Components

- **epicprod** — automated ePIC production through PanDA; the source of produced
  data and of the availability signal.
- **PCS** — Physics Configuration System; the configuration and campaign record.
- **Hydra** — the ePIC validation application; the evaluator. Runs the validation
  benchmarks over completed samples and records the results.
- **argus-ai** — the assessment application; assesses a target and returns a
  natural-language result. See
  [argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).

## The loop

```
PanDA processing brings a sample to its event count target
  → epicprod calls the Hydra REST endpoint: sample complete
    → Hydra runs the validation benchmarks and evaluates the sample
      → epicprod reads the validation result
          validated → the sample is marked validated
          failed    → the data is invalidated, the delivered event count
                      drops, and production resumes (after human gate) to restore it
```

LLM assessment sits alongside the loop, applied to a validation outcome when a
natural-language reading of it is wanted (see Assessment below).

## Availability signal (epicprod → Hydra)

Production runs task by task; PanDA monitors task processing and determines
completion. The unit of validation is the sample — a task/dataset with an event
count target.

Sample completion is event-based: a sample is complete when its delivered event
count reaches its event count target. The catalog today records file-level
completeness only; the event basis — delivered event counts from production and
per-sample event count targets — is in development on both sides.

When a sample is complete, epicprod signals its availability for validation by
calling a REST endpoint provided by Hydra. Separately, epicprod serves the
campaign catalog: a complete machine-readable description of a campaign — for
each sample, its configuration tags, physics and generator context, originating
request, status, and the produced Rucio data references
([EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md)) with completeness. The
catalog is described in [PCS.md](PCS.md).

## Benchmarks

The Validation Working Group selects the benchmarks that run as the automated
post-campaign validation step, triggered on sample completion. Statistics
requirements differ per benchmark — physics analyses validate at different
levels of statistics — and those requirements, provided by the physics groups,
inform each sample's event count target.

## Hydra

Hydra takes the availability signal and the produced-data references, runs the
selected benchmarks, and records the validation result. Hydra has no LLM
interface; it provides a mechanism for chat interaction with its database.

## Validation results (Hydra → epicprod)

epicprod obtains validation results per sample and campaign from a validation
interface — web page or REST JSON — to complete the automated "is validated"
step of the production workflow and mark the produced data validated. The same
interface serves other consumers of validation outcomes, LLM assessment among
them.

A sample that fails validation is invalidated: the failed data no longer counts
toward the sample, its delivered event count drops below the target, and the
campaign continues or resumes production to restore the event count target,
once an ops person has approved the continuation. 

## REST interface (proposed)

The two loop interfaces are REST endpoints hosted by Hydra under a common
`/api/v1` base, authenticated by bearer token. epicprod provides two
counterparts: a completion read so validation can also pull, and a result
notification endpoint Hydra transmits finished results to. Samples are
identified by the PCS composed name ([PCS.md](PCS.md)).

### Availability signal endpoint

    POST /api/v1/samples/{sample}/complete

```json
{
  "sample": "group.EIC.26.07.1.epic_craterlake.p2339.e1.s1.r1",
  "campaign": "26.07.1",
  "revision": 1,
  "events_delivered": 5000000,
  "event_target": 5000000,
  "rucio": ["group.EIC:group.EIC.26.07.1.epic_craterlake.p2339.e1.s1.r1.b1"],
  "catalog_url": "https://.../pcs/catalog/?campaign=26.07.1"
}
```

- `revision` increments each time the sample re-completes after a failed
  validation invalidated data and production restored the event count, so a
  sample can be validated more than once, each result tied to the delivery it
  judged. The request is idempotent per (sample, revision).
- `rucio` lists the produced data the benchmarks run over, as Rucio dataset
  references ([EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md));
  `catalog_url` points to the complete campaign description, so the signal
  itself stays small.
- Response: `202` with a `validation_id`.

The request does not select benchmarks: the benchmark set for a sample is
Validation Working Group configuration within Hydra, and the statistics
requirements that inform event count targets reach PCS upstream of this
interface.

### Validation results endpoints

    GET /api/v1/samples/{sample}/validation        latest result; ?revision=N for history
    GET /api/v1/campaigns/{campaign}/validation    per-sample summary for a campaign

```json
{
  "sample": "group.EIC.26.07.1.epic_craterlake.p2339.e1.s1.r1",
  "revision": 1,
  "status": "validated",
  "benchmarks": [
    {"name": "dis-nc-kinematics", "status": "passed", "events_used": 1000000,
     "report_url": "https://hydra.../reports/..."}
  ],
  "invalidated": [],
  "completed_at": "2026-08-11T14:00:00Z",
  "details_url": "https://hydra.../samples/group.EIC.26.07.1.epic_craterlake.p2339.e1.s1.r1"
}
```

- `status` is one of `pending`, `running`, `validated`, `failed`; the
  automated "is validated" step of the production workflow reads this field.
- On `failed`, `invalidated` lists the Rucio references that no longer count
  toward the sample; an empty list means the entire sample is invalidated.
  epicprod reduces the delivered event count accordingly, and production
  resumes after the operator gate.
- `details_url` is the corresponding Hydra web page, the web view of the same
  result.

### Result notification

    POST /api/v1/validation-results        (epicprod-hosted)

epicprod provides this endpoint; Hydra transmits the validation result JSON
above to it when a validation completes, so the production workflow acts on
the outcome without polling. The Hydra GET endpoints remain the authoritative
record of validation results.

### Completion pull (epicprod-hosted)

epicprod serves the same completion state for validation to read, so Hydra
can pull on its own schedule in addition to, or instead of, acting on the
push signal:

    GET /api/v1/samples/{sample}/completion        completion state for a sample
    GET /api/v1/campaigns/{campaign}/completion    per-sample completion for a campaign

The per-sample body is the availability-signal JSON; the campaign form lists
that body per sample. These are open read-only endpoints. For anything beyond
completion state — a sample's physics process, generator configuration,
originating request — the campaign catalog ([PCS.md](PCS.md)) serves the
complete campaign description, and every completion body carries its URL.

## Assessment

Validation outcomes can serve LLM assessments; argus-ai will be one mechanism
for a natural-language judgment. An assessment of a validation — a
natural-language judgment with history and benchmark comparison — can be
requested when the validation result needs interpretation. A user request via
DISpatcher passes to argus-ai through corun-ai's MCP service and returns the
assessment. Whether assessments also run automatically on new validations is a
per-source, per-target setting, so the assessment rate stays under operator
control. The assessment itself — its inputs, execution, and history and
benchmark comparison — is described in
[argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).

One assessment can cover a single sample or a group of them — a request or a
benchmark — independent of the per-sample availability signal.

## Delivery

When an assessment completes, argus-ai delivers the result to the destinations
registered for that request: Mattermost via DISpatcher, and any registered REST
endpoints. The requestor is recorded.

## Validation track

Validation and its assessment are part of the production workflow, visible
across the loop and recorded against the sample.

## Next steps

- Hydra: establish ePIC as a full member experiment; identify a deployment host.
- Validation: trigger an example benchmark workflow from the production
  availability signal.
- Production: define per-sample event count targets and the PanDA-driven
  completion signaling to validation.

## Related

- [PCS.md](PCS.md) — the configuration and campaign record.
- [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) — produced-dataset Rucio references; the availability signal draws on these.
- [argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md) — the assessment application.
