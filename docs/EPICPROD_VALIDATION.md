# ePIC Production Validation

ePIC production validation connects three systems. epicprod runs automated ePIC
production through PanDA, producing simulation and reconstruction data. Hydra, the
ePIC validation application, is the evaluator: it runs validation benchmarks over
completed samples and records the results. argus-ai provides natural-language
assessment of a validation when deeper analysis is wanted. This document describes
the loop and the two interfaces that join the systems: the sample-completion
signal from epicprod to validation, and the validation result read back by
epicprod to confirm the sample.

This records the direction agreed between the production and validation groups;
both interfaces are in development.

The assessment application is described in
[argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).
The loop draws on the produced-data references in
[EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) and the configuration record in
[PCS.md](PCS.md).

## Components

- **epicprod** — automated ePIC production through PanDA; the source of produced
  data and of the sample-completion signal.
- **PCS** — Physics Configuration System; the configuration and campaign record.
- **Hydra** — the ePIC validation application; the evaluator. Runs the validation
  benchmarks over completed samples and records the results.
- **argus-ai** — the assessment application; assesses a target and returns a
  natural-language result. See
  [argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).

## The loop

```
PanDA processing brings a sample to its target event count
  → epicprod calls the validation REST endpoint: sample complete
    → Hydra runs the validation benchmarks and evaluates the sample
      → epicprod reads the validation result
          validated → the sample is confirmed
          failed    → the data is invalidated and production resumes
                      to restore the sample to its target event count
```

argus-ai assessment sits alongside the loop, applied to a validation when a
natural-language reading of it is wanted (see Assessment below).

## Sample completion (epicprod → validation)

Production runs task by task; PanDA monitors task processing and determines
completion. The unit of validation is the sample — a task/dataset with a target
event count.

Sample completion is event-based: a sample is complete when its processed event
count reaches its target event count. The catalog today records file-level
completeness only; the event basis — processed event counts from production and
per-sample target event counts — is in development on both sides.

When a sample is complete, epicprod notifies validation by calling a REST
endpoint provided by Hydra. The campaign-catalog JSON remains the comprehensive
pull-side view of the campaign: for each task/dataset, its configuration tags,
campaign, request, status, and the produced Rucio references
([EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md)) with completeness. The
catalog is described in [PCS.md](PCS.md).

## Benchmarks

The Validation Working Group selects the benchmarks that run as the automated
post-campaign validation step, triggered on sample completion. Statistics
requirements differ per benchmark — physics analyses validate at different
levels of statistics — and those requirements, provided by the physics groups,
inform each sample's target event count.

## Hydra

Hydra takes the sample-completion notification and the produced-data references,
runs the selected benchmarks, and records the validation result. Hydra has no
LLM interface; it provides a mechanism for chat interaction with its database.

## Validation result (validation → epicprod)

epicprod obtains validation results per sample and campaign from a validation
interface — web page or REST JSON — to complete the automated "is validated"
step of the production workflow.

A sample that fails validation is invalidated: the failed data no longer counts
toward the sample, its completed event count drops below target, and the
campaign continues or resumes production to restore the target event count.

## Assessment (argus-ai)

An assessment of a validation — a natural-language judgment with history and
benchmark comparison — can be requested when the validation result needs
interpretation. A user request via DISpatcher passes to argus-ai through
corun-ai's MCP service and returns the assessment. Whether assessments also run
automatically on new validations is a per-source, per-target setting, so the
assessment rate stays under operator control. The assessment itself — its
inputs, execution, and history and benchmark comparison — is described in
[argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).

One assessment can cover a single sample or a group of them — a request or a
benchmark — independent of the per-sample completion signal.

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
  sample-completion signal.
- Production: define per-sample target event counts and the PanDA-driven
  completion signaling to validation.

## Related

- [PCS.md](PCS.md) — the configuration and campaign record.
- [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) — produced-dataset Rucio references; the completion signal draws on these.
- [argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md) — the assessment application.
