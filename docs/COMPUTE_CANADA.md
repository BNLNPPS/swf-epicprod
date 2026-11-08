# Compute Canada: the Fir ARC compute elements

Fir is a cluster of the Digital Research Alliance of Canada (DRAC) at Simon
Fraser University, grid site `CA-SFU-T2`. Its two ARC compute elements (CEs)
take work submitted directly from the PanDA harvester host, outside the OSG
pool. This document records how a submission reaches them, how it
authenticates, the probe that tests the route, and the state of the
integration. The PanDA queue planned for it is `ComputeCanada_PanDA_1`; it is
not yet defined.

Fir also contributes glidein slots to the OSG pool, where they appear as the
execute site `ComputeCanada-Fir` and run the ordinary OSG pilots
([OSG_SUBMISSION.md](OSG_SUBMISSION.md)). That route needs nothing described
here.

## Allocation and storage

ePIC's Canadian allocation is requested through DRAC's Research Platforms and
Portals (RPP) program, which requires utilization above 50% of the requested
resources for most of the time, without the bursting a fairshare allocation
allows. A queue on Fir therefore needs a steady base load, which is what the
pressure front of [CONTINUOUS_PRODUCTION.md](CONTINUOUS_PRODUCTION.md)
supplies.

ePIC has no DRAC storage. Jobs on Fir write their outputs to JLab and BNL, as
at every other site.

## The compute elements

| CE | ARC endpoint |
|---|---|
| `lcg-atlas-ce1.fir.alliancecan.ca` | `https://lcg-atlas-ce1.fir.alliancecan.ca:443/arex` |
| `lcg-atlas-ce2.fir.alliancecan.ca` | `https://lcg-atlas-ce2.fir.alliancecan.ca:443/arex` |

Both run ARC's A-REX service with its REST interface (`/arex/rest/1.0/`). The
batch queue used for tests is `cpubase_bycore_b4`. The CE host certificates are
issued by a grid (IGTF) certification authority: a client verifies them
against `/etc/grid-security/certificates` (curl's `--capath`), and the system
CA bundle rejects them. The REST `info` endpoint answers without
authentication, so a 200 from it shows only that the CE is reachable.

## Authentication

A submission carries a WLCG token from the PanDA IAM instance (issuer
`https://panda-iam-doma.cern.ch/`), minted with the harvester's compute client
for one CE as its audience, with the compute scopes. On pandaharvester01, as
`atlpan`:

```
cd /data/atlpan/tokens/panda-tokens-for-ce
./venv/bin/python get-panda-iam-token.py -c ./token_harvester-compute_client.json \
    --audience lcg-atlas-ce2.fir.alliancecan.ca \
    --scope "compute.read compute.modify compute.create compute.cancel" \
    -o /data/atlpan/fir-test/fir-ce2.token
```

The token's subject is the compute client's identity,
`fe4a61df-87b1-418b-8fd1-fd8a50eb4394`; it carries no group claim. A CE
authorizes a submission by matching that issuer and subject to an
authorization group, mapping the group to a local account, and admitting the
group to the jobs interface; site administration configures all three on each
CE (State).

## The probe

[`computecanada/fir-probe.sh`](../computecanada/fir-probe.sh) reports what a
pilot would find on a Fir node and changes nothing: the identity and batch
environment, the cores and memory limit, the operating system, the CVMFS
repositories, Apptainer and an image start from
`singularity.opensciencegrid.org`, HTTPS reach of the PanDA server and the BNL
XRootD door, and the free disk.
[`computecanada/fir-probe.sub`](../computecanada/fir-probe.sub) submits it
from the harvester host's HTCondor as an ARC grid job (one slot, 2 GB, one
hour) with the token above, to ce2;
[`computecanada/fir-probe-ce1.sub`](../computecanada/fir-probe-ce1.sub) is the
same to ce1, with a token minted for ce1 (`fir-ce1.token`). The copies in use are in `/data/atlpan/fir-test/`
on pandaharvester01; after minting a fresh token there:

```
cd /data/atlpan/fir-test && condor_submit fir-probe.sub
```

HTCondor pings the CE before it submits. A refused ping writes "Detected Down
Grid Resource" to the job's user log (`fir-probe.<cluster>.log`) and leaves the
job idle; HTCondor keeps retrying the CE, so the job goes through when the CE
accepts the token, and the submit description removes it after a day idle. The
CE's answer is the `ARC_PING` result for its URL in
`/var/log/condor/GridmanagerLog.atlpan`. A job that ran leaves the node facts
in `fir-probe.<cluster>.out`.

## State

Both CEs are reachable from pandaharvester01 over TLS with the grid
certification authorities, and both refuse the submission. The probes of
6 November 2026, 13:56 ET (clusters 29079 and 29080), were refused by both.
On 8 November, 10:48 ET, with fresh tokens, ce2 still answered the REST job
and delegation calls with `500 User can't be assigned configuration`, and ce1
answered them with `403 Operation is not allowed`, body "No supported identity
found": ce1 no longer accepts the token as an identity.
A-REX's answer carries its reason in the HTTP status line: `GET /arex` gets
`403 User can't be assigned configuration` and the REST job and delegation
calls get `500 User can't be assigned configuration`. Read the status line, not
only the code (`curl -D -`): the 403 and 500 bodies are empty.

The token passes authentication and mapping. Site administration configured

```
[authgroup: iam_epic_group]
authtokens = fe4a61df-87b1-418b-8fd1-fd8a50eb4394 https://panda-iam-doma.cern.ch/ * * *

[mapping]
map_to_user = iam_epic_group epicprod:rrg-wdconinc
```

and the CE log of 27 October shows the request assigned to `iam_epic_group`
and mapped to the local account `epicprod`, then "Can't obtain configuration.
Only public information is provided." and the 403, with no error logged. In
ARC 7, A-REX gives a request its configuration only when one of the request's
authorization groups matches an `allowaccess` entry of `[arex/ws/jobs]`
([`ARexService::preProcessSecurity`](https://source.coderefinery.org/nordugrid/arc/-/blob/v7.2.0/src/services/a-rex/arex.cpp#L366));
a request that matches none gets exactly this answer. The change remaining on
each CE is

```
[arex/ws/jobs]
allowaccess = iam_epic_group
```

with the same line in the batch queue's block, `[queue:cpubase_bycore_b4]`,
where that block has `allowaccess` lines. The CEs do not publish their ARC
version; under ARC 6, a refusal after mapping logs "Failed to acquire A-REX's
configuration", which the log does not show.

Requests from pandaharvester01 leave over IPv6, from `2620:12f:f001:1::17`, to
`2607:f8f0:690:3::127:72` (ce2) and `2607:f8f0:690:3::127:71` (ce1); a search
of the site's logs by the host's IPv4 address finds nothing.

## Building the queue

Once the probe runs, `ComputeCanada_PanDA_1` is built as the other harvester
queues are: a queue in the harvester configuration
(`panda_queueconfig.json` on pandaharvester01) whose submit description sends
pilots to the CEs through ARC with the token, the queue record in the tracked
schedconfig overrides ([SCHEDCONFIG_OVERRIDES.md](SCHEDCONFIG_OVERRIDES.md)),
the native pilot launcher ([NATIVE_PILOT_CANARY.md](NATIVE_PILOT_CANARY.md)),
and an entry in the queue registry ([QUEUE_REGISTRY.md](QUEUE_REGISTRY.md)). A
candidate ARC submit description is on the harvester host,
`/data/atlpan/harvester_common/submit_pilot2_push_arc.sdf` (ARC REST, token
plus delegated proxy); no queue uses it.
