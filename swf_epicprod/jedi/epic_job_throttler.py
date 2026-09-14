"""The ePIC job throttler's decision (docs/EPIC_JOB_THROTTLER.md).

A pure function of the per-site job statistics and the configuration,
with no PanDA dependency, so it is tested without a server. The
JEDI-facing engine that gathers the inputs and returns the answer to
the job generator is ``EpicProdJobThrottler`` in this package.

ePIC tasks are pinned to one site each, so the ATLAS engine's count
over a whole work queue puts a saturated site and a starved one in the
same group; this decision reads the same statistics per site and
applies the ATLAS rule to each: a site is saturated when its queued
jobs (assigned, activated, starting, defined) exceed the larger of
``THROTTLE_THRESHOLD`` times its running jobs and ``NQUEUELIMIT``, or
when a cap on running or queued jobs is exceeded. The work queue is
throttled when every site is saturated; otherwise the pass is capped at
the room of the unsaturated sites and the saturated sites are named
for exclusion from task selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The ATLAS engine's per-pass bound on generated jobs: at most 600 jobs a
# bunch over two parallel generators.
PASS_MAX_JOBS = 300
# The ATLAS engine's queued floor when no NQUEUELIMIT is set: four bunches
# of jobs, at the bunch size it uses when nothing runs.
DEFAULT_NQUEUELIMIT = 4 * 500
DEFAULT_THRESHOLD = 2.0
# Below this fraction of the queued floor the generator is told it lacks
# jobs and fills in parallel, as the ATLAS engine does.
LACK_FRACTION = 0.9

NOT_RUN_STATES = ("assigned", "activated", "starting")
CONFIG_TAGS = ("THROTTLE_THRESHOLD", "NQUEUELIMIT", "NRUNNINGCAP", "NQUEUECAP")


@dataclass
class SiteReading:
    """One site's counts for the resource type under decision, and the
    limits it is held to."""

    site: str
    running: int = 0
    not_run: int = 0
    defined: int = 0
    threshold: float = DEFAULT_THRESHOLD
    nqueuelimit: int = DEFAULT_NQUEUELIMIT
    nrunningcap: int | None = None
    nqueuecap: int | None = None

    @property
    def queued(self) -> int:
        return self.not_run + self.defined

    @property
    def bound(self) -> float:
        """The queued level the site is held to."""
        return max(self.threshold * self.running, self.nqueuelimit)

    @property
    def room(self) -> int:
        return max(0, int(self.bound - self.queued))

    def saturation(self) -> str | None:
        """Why the site is saturated, or None when it is not."""
        if self.nrunningcap is not None and self.running > self.nrunningcap:
            return f"running {self.running} > NRUNNINGCAP {self.nrunningcap}"
        if self.nqueuecap is not None and self.queued > self.nqueuecap:
            return f"queued {self.queued} > NQUEUECAP {self.nqueuecap}"
        if self.queued > self.bound:
            return f"queued {self.queued} > max({self.threshold} x running {self.running}, NQUEUELIMIT {self.nqueuelimit})"
        return None


@dataclass
class Decision:
    throttled: bool
    max_num_jobs: int | None
    excluded_sites: list[str]
    lack_of_jobs: bool
    lines: list[str] = field(default_factory=list)


def decide(readings: list[SiteReading]) -> Decision:
    """The answer over the sites of one work queue and resource type.

    Throttled when every site is saturated. Otherwise unthrottled, with
    the pass capped at the room of the unsaturated sites (bounded by the
    ATLAS per-pass maximum) and the saturated sites named. With no site
    at all, unthrottled and uncapped: nothing is queued anywhere.
    """
    lines: list[str] = []
    saturated: list[str] = []
    room = 0
    total_queued = 0
    total_floor = 0
    for r in sorted(readings, key=lambda x: x.site):
        why = r.saturation()
        total_queued += r.queued
        total_floor += r.nqueuelimit
        if why:
            saturated.append(r.site)
            lines.append(f"{r.site}: SATURATED {why}; running={r.running} queued={r.queued}")
        else:
            room += r.room
            lines.append(f"{r.site}: room {r.room} (bound {r.bound:.0f}, queued {r.queued}, running {r.running})")
    if not readings:
        return Decision(False, None, [], True, ["no site has jobs: unthrottled"])
    if len(saturated) == len(readings):
        return Decision(True, None, saturated, False, lines + ["every site saturated: THROTTLED"])
    max_num_jobs = min(room, PASS_MAX_JOBS)
    lack = total_queued < total_floor * LACK_FRACTION
    lines.append(f"unthrottled: max_num_jobs={max_num_jobs} excluded={saturated or '[]'} lack_of_jobs={lack}")
    return Decision(False, max_num_jobs, saturated, lack, lines)


def readings_from_stats(
    stats: dict[str, dict[str, dict[str, int]]],
    resource_name: str,
    site_config: dict[str, dict[str, Any]],
) -> list[SiteReading]:
    """Fold ``getJobStatisticsByResourceTypeSite`` output and per-site
    configuration into readings, at the resource-type level.

    ``stats`` is ``{site: {resource_type: {status: count}}}``. A site is
    read when it has jobs of the resource type; ``site_config`` carries
    the limits of the sites that have one, keyed by ``CONFIG_TAGS``.
    """
    readings: list[SiteReading] = []
    sites = sorted(s for s, by_rt in stats.items() if resource_name in by_rt)
    for site in sites:
        by_status = stats[site][resource_name]
        cfg = site_config.get(site, {})
        readings.append(
            SiteReading(
                site=site,
                running=int(by_status.get("running", 0) or 0),
                not_run=sum(int(by_status.get(s, 0) or 0) for s in NOT_RUN_STATES),
                defined=int(by_status.get("defined", 0) or 0),
                threshold=float(cfg.get("THROTTLE_THRESHOLD", DEFAULT_THRESHOLD)),
                nqueuelimit=int(cfg.get("NQUEUELIMIT", DEFAULT_NQUEUELIMIT)),
                nrunningcap=cfg.get("NRUNNINGCAP"),
                nqueuecap=cfg.get("NQUEUECAP"),
            )
        )
    return readings
