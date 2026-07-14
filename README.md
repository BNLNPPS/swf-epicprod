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

PCS documentation:

- [PCS.md](docs/PCS.md) — the system: tags, datasets, composed
  identities, campaigns, production configs, REST and MCP surfaces.
- [PCS_DATASET_REQUEST_WORKFLOW.md](docs/PCS_DATASET_REQUEST_WORKFLOW.md)
  — production request intake and the dataset request workflow.
- [PCS_BACKGROUND_TAG.md](docs/PCS_BACKGROUND_TAG.md) — the background
  tag axis.

The epicprod documentation set lives in [`docs/`](docs/). The campaign
assessments design — the campaign analytics library, its rollup
service, and the scheduled assessments — is
[EPICPROD_ASSESSMENTS.md](docs/EPICPROD_ASSESSMENTS.md). The master
production dashboard plan is
[EPICPROD_DASHBOARD.md](docs/EPICPROD_DASHBOARD.md).

The official system-level documentation of the ePIC WFMS is
<https://epic-wfms-docs.readthedocs.io>.
