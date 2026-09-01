# ePIC Production on the OSG Pool

How ePIC production jobs reach the Open Science Grid (OSG): the hosts,
the harvester and HTCondor configuration that submits the pilots, the
pool they run in, the container layering that results, and the levers
that are under production operations control. Facts below were read
from the live hosts and the pool on 2026-09-01; the dated snapshots
should be refreshed when the configuration changes.

## Hosts and roles

Two harvester instances submit ePIC pilots, on two SCDF hosts.

| Host | Harvester id | Queues served | Submits to |
|---|---|---|---|
| `pandaharvester01.sdcc.bnl.gov` | `BNL_harvester_1` | BNL_EPIC_PROD_1, BNL_PanDA_1, E1_BNL, E1_JLAB, UM_GREX_PanDA_1, BNL_OSG_2 | HTCondor-CEs named in CRIC per queue (BNL spoolce01/02, the Manitoba GREX CE) |
| `osgsub01.sdcc.bnl.gov` | `BNL_osg_harvester_1` | BNL_OSG_EPIC_PROD_1, BNL_OSG_PanDA_1, BNL_OSG_PanDA_CI, BNL_OSG_PanDA_pilotest, BNL_OSG_PanDA_test | the OSG pool directly, through a local HTCondor schedd |

Both run harvester as user `atlpan` under `/opt/harvester`, with the
live queue configuration at `/opt/harvester/etc/panda/panda_queueconfig.json`
and the submit templates, pilot wrapper, and proxy under
`/var/data/atlpan/harvester_common/` (osgsub01) or
`/data/atlpan/harvester_common/` (pandaharvester01). No glideinWMS
component runs on either host.

Production operations (`wenauseic`) has passwordless root on both
since 2026-09-01, alongside pandaserver01. The PanDA service
maintainer is told in advance of any production-impacting change;
every edit leaves a dated backup beside the file and is recorded here.

## The OSG queue's submission path

`BNL_OSG_EPIC_PROD_1` is defined in CRIC with no compute element,
`catchall` `osgpool=true`, pilot manager Harvester, workflow `push`.
On osgsub01 it inherits the `production.push` template queue
(HTCondorSubmitter, one worker per job, `truePilot`, CRIC-driven) with
its own submit description
`submit_pilot2_push_bnl_osg.sdf` and the BNL pilot wrapper
`bnlpanda.runpilot2-wrapper.sh` (version 20250605a-eic) as the
executable; worker ceiling 10000, 2000 new workers per cycle.

The wrapper fetches pilot3 from CVMFS
(`/cvmfs/eic.opensciencegrid.org/panda/pilot/pilot3.tar.gz`) and runs
it with `-e eic --pilot-user epic --harvester-submit-mode PUSH` against
pandaserver01, BNL Rucio for traces off. Condor logs are served at
`https://osgsub01.sdcc.bnl.gov/condor_logs/`.

The schedd's collectors are `jlab-cm.osg.chtc.io` and
`scicollector901.jlab.org`: the pool is the JLab glideinWMS pool with
an OSG-hosted central manager. The slot classads name what provisions
it: the OSG production factories (`GLIDEIN_Factory` NRP-Prod and
Tiger-Prod) serving the JLab VO frontend (`GLIDECLIENT_Name`
`JLabVO-1_0.clas12` and `JLabVO-1_0.gluex`), through entries created
for the CLAS12 and GlueX experiments (`CLAS12_T3_UK_ScotGrid_GLA`,
`CLAS12_T1_IT_CNAF_*`, `CMSHTPC_T2_US_MIT`, `OSG_US_UConn_gluskap`,
`Glow_US_Syracuse`, `OSG_US_JLAB_ce-2`, `CMSHTPC_T2_US_UCSD`,
`CMSHTPC_T2_FR_GRIF_LLR`). There is no EIC frontend group and no
EIC entry: ePIC pilots run as passengers in the pool the JLab VO
frontend requests for CLAS12 and GlueX, which is why the site mix is
theirs. No glideinWMS component runs on the BNL hosts; the frontend is
the JLab VO's, hosted with OSG, and its configuration is reachable only
through JLab's OSG contacts.

## The submit description

The attributes of `submit_pilot2_push_bnl_osg.sdf` that shape the
pilots (2026-09-01):

```
+ProjectName = "EIC"
+JobDurationCategory = "Medium"
request_cpus = {nCoreTotal}; request_memory = {requestRam}; request_disk = {requestDisk}
Requirements = (HAS_CVMFS_atlas_cern_ch == True) && (OSGVO_OS_STRING == "RHEL 9")
               && (HAS_UNPRIVILEGED_USER_NAMESPACES =?= "enabled")
+UNDESIRED_Sites = "UCSD, FNAL, MI-HORUS, GREX, BNL-SDCC"
periodic_remove = (JobStatus == 2 && (CurrentTime - EnteredCurrentStatus) > 604800)
```

No `+SingularityImage` is set, so the glidein starts the pilot in its
own default image (below). The 2026-08-11/13 revision added the
user-namespace requirement and the undesired-site list to the
2026-03-17 version, and changed the pilot user from `eic` to `epic`.

## The pool as advertised

Slot classads on 2026-09-01 (7410 slots):

| Site | Slots | OS | Unprivileged user namespaces | ATLAS CVMFS |
|---|---|---|---|---|
| SGridGLA | 2534 | RHEL 9 | enabled | yes (28 without) |
| CNAF | 1505 | RHEL 9 | enabled | yes |
| MIT | 1135 | RHEL 9 | enabled | yes |
| UConn | 1065 | RHEL 9 (99 RHEL 8, Debian, Ubuntu) | enabled | yes |
| SU-ITS | 719 | RHEL 9 | unavailable | mostly |
| JLab-FARM-CE | 377 | RHEL 9 | enabled | no |
| UCSD | 71 | RHEL 9 and 8 | enabled | yes |
| GRIF_LLR | 4 | RHEL 9 | enabled | no |

Container policy is uniform: `GLIDEIN_Singularity_Use = PREFERRED`,
`GLIDEIN_SINGULARITY_REQUIRE = OPTIONAL`, image restriction `cvmfs`
on 6968 slots and none on 426, apptainer in unprivileged mode except
SU-ITS (privileged). The image dictionary's default for RHEL 9 is
`/cvmfs/singularity.opensciencegrid.org/opensciencegrid/osgvo-el9:latest`.
PREFERRED with OPTIONAL means a job runs in the default image unless it
names its own with `+SingularityImage`; any image under
`/cvmfs/singularity.opensciencegrid.org` satisfies the restriction.

The current requirements admit 6177 of the 7410 slots. Excluded: SU-ITS
(no unprivileged user namespaces), JLab-FARM-CE and GRIF_LLR (no ATLAS
CVMFS), and the non-RHEL 9 slots. Pool membership changes daily; in the
24 hours to 2026-09-01 16:00 ET the pilots ran at MWT2, Indiana,
Alliance Canada, UConn, Illinois, Wisconsin, UW-Milwaukee, INFN, and
FSU.

## Container layering as implemented

1. The glidein starts the pilot job inside the pool's default image
   (`osgvo-el9`), because the submit description names none.
2. The BNL wrapper runs bare inside it. Its own apptainer step
   (`sing_cmd`, the ATLAS almalinux9 image) is taken only when the CRIC
   `container_type` is `singularity:wrapper`; the ePIC queues carry
   `apptainer:pilot`, so it is skipped. The wrapper does use ATLAS CVMFS
   for its python3 (ALRB).
3. The pilot's epic module (`pilot/user/epic/container.py`) always
   wraps the payload with ALRB: `setupATLAS -c <image>` launches
   `eic_xl` as a second, nested apptainer. Pilot `Container.setup_type`
   is `ALRB` and the epic module has no raw-apptainer path and no
   detection that it is already inside the requested image.
4. The payload runs in the nested `eic_xl`.

The nesting is why the submit description requires unprivileged user
namespaces, and the ALRB launch is why it requires ATLAS CVMFS. Where
either is absent the job cannot run; this is the mechanism behind the
2026-07 failures at sites with user namespaces disabled, and behind the
exclusion of the JLab farm.

## Activity

PanDA record for BNL_OSG_EPIC_PROD_1 on 2026-09-01: 2305 jobs in the
last 24 hours, 2198 finished and 107 failed, about 5300 core-hours, six
production tasks; the seven-day total is 2237 finished against 650
failed, 541 of the failures on 2026-08-26 with nothing finishing that
day. The queue is opportunistic capacity fed only when tasks target
it; it idles for lack of work rather than lack of slots.

The compute-usage page (`/compute-usage/`) shows the queue as
"OSG Pool (BNL_OSG_EPIC_PROD_1)"; its row expands inline into its
execute sites for the selected period, under a header of its own with
an independent sort: jobs, failures, failure rate, core-hours,
efficiency, and share within the pool. Test queues stay out of the page
until the Test queues tick is on. The execute site is
the glidein site the pilot records on the job: since the 2026-08-13
change of the pilot user to `epic`, the pilot's epic module reads
`GLIDEIN_Site` from the glidein's machine ad (`_CONDOR_MACHINE_AD`) and
reports it as the job's `destinationsite` (the submit host as
`sourcesite`). Records that predate the report fall back to the
worker-node host's domain. A job whose host is the submit host never
reached a worker node and is listed as not dispatched. In August 2026
the queue's jobs resolved to 24 named sites, among them UChicago
(MWT2), ComputeCanada-Fir, UConn, GREX, CNAF, MI-HORUS, FSU, CHTC,
Alabama-CHPC, FANDM-ITS, Nebraska, Rhodes-HPC, UWM-Mortimer, LIGO-WA,
and BNL-SDCC. The same breakdown is available through the
`panda_resource_usage` MCP tool with `execute_sites`. The pilot's
condor job on osgsub01 also records the site (`OSG_SITE_NAME` in the
wrapper output, `GLIDEIN_Site` in the schedd history), the source the
maintainer's `count_error_sites.sh` reads.

## Levers

Under production operations control now:

- Submit description: `+SingularityImage` (an `eic_xl` image on CVMFS
  is admissible on every slot), `Requirements`, `+UNDESIRED_Sites`,
  `+JobDurationCategory`, resource requests.
- Harvester queue configuration: worker limits, push versus pull
  (with the CRIC `workflow` field), the template and wrapper used.
- The pilot (pilot3, `pilot/user/epic/`): a stand-down when the
  requested image is already the running container, and independence
  from ALRB.
- The payload and the submission spec on the production side.

Not under local control: the pool's frontend policy and factory
entries. They belong to the JLab VO frontend, whose groups are CLAS12
and GlueX; an EIC group in that frontend, or an EIC frontend of its own,
is what would give ePIC its own glidein policy and site list.

## Plan

1. Baseline: a canary probe to the queue as configured, recording what
   the pilot sees (site-canary).
2. `+SingularityImage = eic_xl` on the submit description together
   with the pilot stand-down, so the glidein's container is the only
   one. Verified by probe.
3. With nesting gone, drop the user-namespace requirement; with the
   pilot off ALRB, drop the ATLAS CVMFS requirement. Each admits the
   slots listed above.
4. Trial pull mode for the queue.

## Related

- [EPICPROD_OPS_AGENT.md](EPICPROD_OPS_AGENT.md): the production
  operations agent, which will run the submit-side changes.
- [PANDA_CAPABILITIES.md](PANDA_CAPABILITIES.md): worker provisioning
  on the other queue classes.
- site-canary `docs/PLAN.md`: the probe used to measure each step.
