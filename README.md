# swf-epicprod

epicprod — the ePIC automated production system — is the production
domain of the swf platform, and this repository is its home: a peer
application of `swf-testbed`, holding the production-specific
applications and documentation.

The platform epicprod runs on has two homes, and this repository
deliberately contains neither: `swf-common-lib` provides the importable
library layer (agent base, message bus, logging), and `swf-monitor`
provides the common monitor, web, and database services (web face,
REST, MCP server, SSE relay, action-stream machinery, system
configuration, alarms engine). Production code here ships as
installable Django applications consumed by the swf-monitor runtime
through the shared virtual-environment chain, together with the
production documentation set.

Platform components are consumed through their interfaces — REST, MCP,
the message bus, or an `swf_common_lib` import — never by importing
another application's source.

The architecture map, [`docs/ARCHITECTURE_MAP.md`](docs/ARCHITECTURE_MAP.md),
records for each platform and production component its current home,
destined home, and consumption interface. Components of the production
domain migrate from swf-monitor to this repository per the map, each
moved document leaving a permanent stub at its old path.

## PCS — Physics Configuration System

PCS, the Physics Configuration System, manages production
configuration and campaign records — physics, event-generation,
simulation, reconstruction, and background tags; datasets and their
composed identities; campaigns and their continuum across the monthly
production cadence; production requests, tasks, and configurations.
PCS is where physicists meet the production system. The `pcs/` Django
application is installed into the swf-monitor runtime.

## Documentation

The epicprod documentation set lives in [`docs/`](docs/). The official
system-level documentation of the ePIC WFMS is
<https://epic-wfms-docs.readthedocs.io>.

Architecture and interfaces:

- [ARCHITECTURE_MAP.md](docs/ARCHITECTURE_MAP.md) — component homes
  and consumption interfaces across the platform.
- [API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md) — the REST API.

PCS:

- [PCS.md](docs/PCS.md) — the system: tags, datasets, composed
  identities, campaigns, production configs, REST and MCP surfaces.
- [PCS_DATASET_REQUEST_WORKFLOW.md](docs/PCS_DATASET_REQUEST_WORKFLOW.md)
  — production request intake and the dataset request workflow.
- [PCS_BACKGROUND_TAG.md](docs/PCS_BACKGROUND_TAG.md) — the background
  tag axis.
- [PCS_COMPOSED_NAME_INTEGRITY.md](docs/PCS_COMPOSED_NAME_INTEGRITY.md)
  — composed-name collision repair and the doors closed against it.
- [PCS_COMPOSED_NAME_FAMILIES.md](docs/PCS_COMPOSED_NAME_FAMILIES.md)
  — the collision-family backfill review.
- [COMMISSIONING_RELAXATIONS.md](docs/COMMISSIONING_RELAXATIONS.md) —
  PCS rules relaxed during commissioning.

Campaigns:

- [CAMPAIGN_CONTINUUM.md](docs/CAMPAIGN_CONTINUUM.md) — the campaign
  continuum across the monthly cadence.
- [CAMPAIGN_FAMILY.md](docs/CAMPAIGN_FAMILY.md) — campaign families.
- [CAMPAIGN_DELIVERY.md](docs/CAMPAIGN_DELIVERY.md) — the delivered-data
  record and its views.
- [STORAGE.md](docs/STORAGE.md) — the placement record: production
  data on every RSE, its lifecycle per RSE, and the Storage view.
- [CONTINUOUS_PRODUCTION.md](docs/CONTINUOUS_PRODUCTION.md) — the ready
  queue, the dispatcher, and the tripwire.
- [EPICPROD_NARRATIVES.md](docs/EPICPROD_NARRATIVES.md) — campaign
  narratives.
- [EPICPROD_ASSESSMENTS.md](docs/EPICPROD_ASSESSMENTS.md) — campaign
  assessments: analytics library, rollup service, scheduled runs.
- [EPICPROD_ASSESSMENTS_V1.md](docs/EPICPROD_ASSESSMENTS_V1.md) —
  assessments V1 implementation plan.
- [EPICPROD_DASHBOARD.md](docs/EPICPROD_DASHBOARD.md) — the master
  production dashboard.

Production operations:

- [EPICPROD_OPS.md](docs/EPICPROD_OPS.md) — operations: running,
  restarting, and monitoring the production services.
- [EPICPROD_OPS_AGENT.md](docs/EPICPROD_OPS_AGENT.md) — the credentialed
  production operations agent.
- [EPICPROD_TASK_CATALOG.md](docs/EPICPROD_TASK_CATALOG.md) — the
  production task catalog.
- [EPICPROD_DATA_LINEAGE.md](docs/EPICPROD_DATA_LINEAGE.md) — produced
  data gathered onto the catalog.
- [EPICPROD_EVGEN_INPUTS.md](docs/EPICPROD_EVGEN_INPUTS.md) — EVGEN
  inputs: assimilation, matching, marks, registration.
- [PCS_INGEST.md](docs/PCS_INGEST.md) — PC ingest: physics
  configurations from legacy submission lines.
- [EPICPROD_QUESTIONNAIRE.md](docs/EPICPROD_QUESTIONNAIRE.md) — the
  production request questionnaire.
- [EPICPROD_VALIDATION.md](docs/EPICPROD_VALIDATION.md) — the validation
  interface.
- [EPICPROD_ALARM_PAUSE.md](docs/EPICPROD_ALARM_PAUSE.md) —
  alarm-driven task pause.
- [EPICPROD_LLM_OPERATIONS.md](docs/EPICPROD_LLM_OPERATIONS.md) — LLM
  operations over epicprod.
- [EPICPROD_SUCCESSION.md](docs/EPICPROD_SUCCESSION.md) — succession:
  credentials, hosts, and hand-over.
- [EPICPROD_PAYLOAD.md](docs/EPICPROD_PAYLOAD.md) — the epicprod
  payload: the in-job runner and payload implemented end to end,
  cloned from the production team's run script and evolved here.
- [RUCIO_RESILIENCE.md](docs/RUCIO_RESILIENCE.md) — preventing Rucio
  registration losses.
- [RUCIO_FAILOVER_STASH.md](docs/RUCIO_FAILOVER_STASH.md) — the failover
  stash: outputs staged at BNL while JLab is unreachable, moved and
  registered at JLab by the registrar.
- [RUCIO_REGISTRATION_CONTRACT.md](docs/RUCIO_REGISTRATION_CONTRACT.md) —
  what every Rucio registration must carry: the event count on every
  file, by registration path.

Submission and execution:

- [JEDI_INTEGRATION.md](docs/JEDI_INTEGRATION.md) — direct task
  submission from PCS to JEDI.
- [JEDI_EPIC_PROPOSAL.md](docs/JEDI_EPIC_PROPOSAL.md) — the original
  direct-submission proposal.
- [OSG_SUBMISSION.md](docs/OSG_SUBMISSION.md) — how production reaches
  the OSG pool: hosts, harvester, submit description, container
  layering, levers.
- [PANDA_USER_JOBS.md](docs/PANDA_USER_JOBS.md) — user jobs and the
  analysis share.
- [PANDA_CAPABILITIES.md](docs/PANDA_CAPABILITIES.md) — PanDA
  capabilities and monitoring for GPU worker jobs.
- [PANDA_ANCILLARY_AUDIT.md](docs/PANDA_ANCILLARY_AUDIT.md) — audit of
  the PanDA ancillary systems.
- [NODE_EVENT_DISPATCHER.md](docs/NODE_EVENT_DISPATCHER.md) —
  event-range processing in fixed-lifetime allocations.
- [WORK_UNIT_CONTRACT.md](docs/WORK_UNIT_CONTRACT.md) — the work-unit
  contract.
- [VOLUNTEER_GPU_PLAN.md](docs/VOLUNTEER_GPU_PLAN.md) — volunteer-class
  GPU computing under PanDA.
- [NPPS0_WORKER.md](docs/NPPS0_WORKER.md) — the GPU worker host behind
  BNL_NPPS_GPU.
- [WINDOWS_WORKER.md](docs/WINDOWS_WORKER.md) — the Windows worker.
- [DEVCLOUD_STAGEOUT.md](docs/DEVCLOUD_STAGEOUT.md) — the devcloud
  stage-out endpoint.
- [DEVCLOUD_SUCCESSION.md](docs/DEVCLOUD_SUCCESSION.md) —
  epic-devcloud.org succession.

Validation records and audits:

- [SYNRAD_VALIDATION.md](docs/SYNRAD_VALIDATION.md) —
  synchrotron-radiation GPU transport validation.
- [ADEPT_AUDIT.md](docs/ADEPT_AUDIT.md) — the AdePT audit.
