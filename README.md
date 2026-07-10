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

The architecture map, `docs/ARCHITECTURE_MAP.md`, records for each
platform and production component its current home, destined home, and
consumption interface. Components of the production domain migrate
from swf-monitor to this repository per the map; the epicprod
documentation set migrates first.

Created 2026-07-10 at the start of the v38 cycle. The first production
content is the campaign assessments work: the campaign analytics
library and its rollup service (design:
[EPICPROD_ASSESSMENTS.md](https://github.com/BNLNPPS/swf-monitor/blob/infra/baseline-v38/docs/EPICPROD_ASSESSMENTS.md),
migrating here with the doc set).

The official system-level documentation of the ePIC WFMS is
<https://epic-wfms-docs.readthedocs.io>.
