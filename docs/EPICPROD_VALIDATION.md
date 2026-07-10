# ePIC Production Validation

ePIC production validation connects three systems. epicprod runs automated ePIC
production through PanDA, producing simulation and reconstruction data. Hydra, the
ePIC validation application, produces validation plots from that data. argus-ai
assesses those plots and returns a natural-language judgment. This document
describes the loop and proposes the two interfaces that join the systems: the
availability signal from epicprod to Hydra, and the assessment handoff from Hydra
to argus-ai.

This should be read as proposal, not established design.

The assessment application is described in
[argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).
The loop draws on the produced-data availability signal in
[EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) and the configuration record in
[PCS.md](PCS.md).

## Components

- **epicprod** — automated ePIC production through PanDA; the source of produced
  data and of the availability signal.
- **PCS** — Physics Configuration System; the configuration and campaign record.
- **Hydra** — the ePIC validation application; produces validation plots.
- **argus-ai** — the assessment application; assesses a target and returns a
  natural-language result. See
  [argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).

## The loop

```
PanDA completes a task/dataset
  → epicprod signals availability (catalog + event)
    → Hydra produces validation plots
      → argus-ai assesses the plots → natural-language judgment
        → delivered to Mattermost and any registered endpoint
```

## Availability (epicprod → Hydra)

Completion is determined by PanDA, which monitors task processing and is aware
of completion. On that completion epicprod signals availability.

The same availability information is offered two ways:

- **Campaign-catalog JSON** — a comprehensive view of the current campaign: for
  each task/dataset, its configuration tags, campaign, request, status, and the
  produced Rucio references ([EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md))
  with file counts and completeness. A consumer reads it
  and compares against its previous read to find what is new and ready to validate.
  The catalog is described in [PCS.md](PCS.md).
- **Live event** — a per-unit notification, the moment a unit becomes available,
  delivered over SSE to subscribers through the swf-remote streaming proxy
  ([SSE_PUSH.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_PUSH.md), [SSE_RELAY.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_RELAY.md)).

The signal is per task/dataset — the unit that completes and can be validated —
delivered as each becomes available. Completeness travels with it (file counts,
expected against actual), so a unit can be offered for validation once it reaches a
chosen threshold.

## Hydra

Hydra takes the availability information and the produced-data references and
returns validation plots.

## Assessment (Hydra → argus-ai)

When a validation is available, Hydra would notify argus-ai, proposing an
assessment of that task/dataset. Whether an assessment then runs automatically is a
per-source, per-target setting, so the assessment rate stays under operator
control. The assessment itself — its inputs, execution, and history and benchmark
comparison — is described in
[argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md).

An assessment can also be triggered by user request via DISpatcher, which passes the
request to argus-ai through corun-ai's MCP service and returns a natural-language
assessment of the validation.

One assessment can cover a single task/dataset or a group of them — a request or a
benchmark — independent of the per-unit availability signal.

## Delivery

When an assessment completes, argus-ai delivers the result to the destinations
registered for that request: Mattermost via DISpatcher, and any registered REST
endpoints. The requestor is recorded.

## Validation track

Validation and its assessment are a first-class part of the production workflow,
visible across the loop and recorded against the task/dataset.

## Related

- [PCS.md](PCS.md) — the configuration and campaign record.
- [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) — produced-dataset Rucio references; the availability signal draws on these.
- [SSE_PUSH.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_PUSH.md), [SSE_RELAY.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_RELAY.md) — the notification mechanism the live event uses.
- [argus-ai.md](https://github.com/BNLNPPS/corun-ai/blob/master/docs/argus-ai.md) — the assessment application.
