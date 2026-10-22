# Production regulation audit and corrective work, 22 October 2026

Torre approved the recommendations following the audit of 21 October's
production dialogue, regulation code and installed configuration. The work
covers task admission, JEDI job generation, pilot supply and the evidence
used to judge those controls. Implementation is in progress; the completed
record below will distinguish deployed changes from remaining work.

## OSG policy

The starting objective is to support 300 pilot starts per minute per
backlogged queue when the pool offers slots, including a transition from
an empty queue. This is a design objective, not a measured pool guarantee.

| Control | Previous setting | Accepted setting |
|---|---|---|
| Waiting pilots | 30% of running, minimum 200, maximum 300 | 600, bounded by activated work |
| Queue lookup | 60 seconds | 20 seconds |
| New workers per cycle | 100 | 300 |
| Worker monitor | 1800 seconds; PanDA_1 override 300 | 30 seconds |
| Condor status cache | 300 seconds | 30 seconds |
| Job-demand cache | 10 minutes | 30 seconds, independently of static records |
| Pilot timefloor | 120 minutes | retained |
| History eligibility | 7200 seconds | 86400 seconds |

At 300 starts per minute, an 80-second nominal observation and submission
budget consumes 400 waiting pilots. A buffer of 600 adds 200 reserve.
The controller cannot guarantee pool slots or a start rate. With 15-minute
jobs, the two-hour timefloor reduces steady pilot replacement demand by
roughly a factor of eight, while cold starts still need new pilots.

`scripts/configure-osg-regulation.py` validates the installed files before
writing, keeps dated backups and changes no worker or job status. Three
small, version-checked compatibility patches let the existing Harvester
cacher refresh only job statistics rapidly, apply the operational monitor
cadence to workers carrying historical queue configurations, and extend
history eligibility for those workers too. The reference Harvester
checkout is unchanged. An upstream upgrade must retain or replace these
patches; incompatible source makes the installer stop before writing.

The existing `panda_harvester-uwsgi` unit is the service activation path.
Queue configuration, service activation and observed controller behavior
are separate verification steps.

## Implementation record

Pending completion.
