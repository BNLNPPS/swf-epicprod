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
for exclusion from task selection. A job generator without site
exclusion (the installed server as of 2026-09-17) generates for the
saturated site's tasks on any unthrottled answer, so for it a
saturated site throttles the work queue.

The statistics come from tables refreshed about once a minute, and a
pass is granted every second or so, so a reading can be re-read many
times before it carries the jobs already granted against it; the
engine keeps a ledger of those grants, and a reading counts them as
pending until it changes.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

# The ATLAS engine's per-pass bound on generated jobs: at most 600 jobs a
# bunch over two parallel generators.
PASS_MAX_JOBS = 300
# The ATLAS engine's queued floor when no NQUEUELIMIT is set: four bunches
# of jobs, at the bunch size it uses when nothing runs.
DEFAULT_NQUEUELIMIT = 4 * 500
DEFAULT_THRESHOLD = 2.0
NOT_RUN_STATES = ("assigned", "activated", "starting")
CONFIG_TAGS = ("THROTTLE_THRESHOLD", "NQUEUELIMIT", "NRUNNINGCAP", "NQUEUECAP")
# The reading a pass is charged to while no site has jobs at all: a pass
# with nothing queued anywhere is not unbounded, it is bounded by the
# default floor and the ledger like any site (2026-09-18: the MCORE
# passes read no site and passed uncapped while the SCORE passes held
# NERSC_Perlmutter_epic saturated; 140,000 jobs generated in 30 minutes).
NO_SITE = "(no site has jobs)"


@dataclass
class SiteReading:
    """One site's counts for the resource type under decision, and the
    limits it is held to."""

    site: str
    running: int = 0
    not_run: int = 0
    defined: int = 0
    # jobs granted against this reading in earlier passes and not yet in it
    granted: int = 0
    threshold: float = DEFAULT_THRESHOLD
    nqueuelimit: int = DEFAULT_NQUEUELIMIT
    nrunningcap: int | None = None
    nqueuecap: int | None = None

    @property
    def queued(self) -> int:
        return self.not_run + self.defined

    @property
    def pending(self) -> int:
        """Queued as read, plus the grants the reading does not carry yet."""
        return self.queued + self.granted

    @property
    def bound(self) -> float:
        """The queued level the site is held to."""
        return max(self.threshold * self.running, self.nqueuelimit)

    @property
    def room(self) -> int:
        return max(0, int(self.bound - self.pending))

    def saturation(self) -> str | None:
        """Why the site is saturated, or None when it is not."""
        if self.nrunningcap is not None and self.running > self.nrunningcap:
            return f"running {self.running} > NRUNNINGCAP {self.nrunningcap}"
        if self.nqueuecap is not None and self.pending > self.nqueuecap:
            return f"queued {self.queued}+{self.granted} granted > NQUEUECAP {self.nqueuecap}"
        if self.pending > self.bound:
            return f"queued {self.queued}+{self.granted} granted > max({self.threshold} x running {self.running}, NQUEUELIMIT {self.nqueuelimit})"
        return None


@dataclass
class Decision:
    throttled: bool
    max_num_jobs: int | None
    excluded_sites: list[str]
    # the sites a granted pass is charged to in the ledger
    granted_sites: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def decide(readings: list[SiteReading], exclusion_honored: bool = False) -> Decision:
    """The answer over the sites of one work queue and resource type.

    Throttled when every site is saturated, or when any site is and the
    generator does not honor the exclusion list (``exclusion_honored``
    False): it would generate for the saturated site's tasks. Otherwise
    unthrottled, with the pass capped at the room of the unsaturated
    sites (bounded by the ATLAS per-pass maximum) and the saturated
    sites named; a pass with no room is throttled, never uncapped. With
    no site at all, unthrottled and uncapped: nothing is queued anywhere.
    """
    lines: list[str] = []
    saturated: list[str] = []
    open_sites: list[str] = []
    room = 0
    for r in sorted(readings, key=lambda x: x.site):
        why = r.saturation()
        if why:
            saturated.append(r.site)
            lines.append(f"{r.site}: SATURATED {why}; running={r.running} queued={r.queued} granted={r.granted}")
        else:
            open_sites.append(r.site)
            room += r.room
            lines.append(f"{r.site}: room {r.room} (bound {r.bound:.0f}, queued {r.queued}, granted {r.granted}, running {r.running})")
    if not readings:
        # Unreachable when the caller charges the no-site reading; kept as
        # the bounded answer for a caller that does not.
        return Decision(False, PASS_MAX_JOBS, [], [NO_SITE], ["no site has jobs: one capped pass"])
    if len(saturated) == len(readings):
        return Decision(True, None, saturated, [], lines + ["every site saturated: THROTTLED"])
    if saturated and not exclusion_honored:
        return Decision(True, None, saturated, [], lines + [f"saturated {saturated} and the generator has no site exclusion: THROTTLED"])
    max_num_jobs = min(room, PASS_MAX_JOBS)
    if max_num_jobs <= 0:
        return Decision(True, None, saturated, [], lines + ["no room at any open site: THROTTLED"])
    lines.append(f"unthrottled: max_num_jobs={max_num_jobs} excluded={saturated or '[]'}")
    return Decision(False, max_num_jobs, saturated, open_sites, lines)


def readings_from_stats(
    stats: dict[str, dict[str, dict[str, int]]],
    resource_name: str,
    site_config: dict[str, dict[str, Any]],
) -> list[SiteReading]:
    """Fold ``getJobStatisticsByResourceTypeSite`` output and per-site
    configuration into readings, one per site over every resource type.

    ``stats`` is ``{site: {resource_type: {status: count}}}``. A site's
    queue is one pool whatever the resource type of the jobs in it, and
    the generator asks once per resource type: a reading per resource
    type let the MCORE pass see an empty site while the SCORE pass held
    it saturated (``resource_name`` is kept for the caller's log line).
    With no site at all the reading is ``NO_SITE`` at the default floor,
    so the pass is charged like any other.
    """
    readings: list[SiteReading] = []
    for site in sorted(stats):
        running = not_run = defined = 0
        for by_status in (stats[site] or {}).values():
            running += int(by_status.get("running", 0) or 0)
            not_run += sum(int(by_status.get(s, 0) or 0) for s in NOT_RUN_STATES)
            defined += int(by_status.get("defined", 0) or 0)
        cfg = site_config.get(site, {})
        readings.append(
            SiteReading(
                site=site,
                running=running,
                not_run=not_run,
                defined=defined,
                threshold=float(cfg.get("THROTTLE_THRESHOLD", DEFAULT_THRESHOLD)),
                nqueuelimit=int(cfg.get("NQUEUELIMIT", DEFAULT_NQUEUELIMIT)),
                nrunningcap=cfg.get("NRUNNINGCAP"),
                nqueuecap=cfg.get("NQUEUECAP"),
            )
        )
    if not readings:
        readings.append(SiteReading(site=NO_SITE))
    return readings


# The ledger of grants not yet in the statistics: one file shared by the
# generator processes, beside the JEDI logs when that directory is
# writable. A grant is charged to a site's reading until the reading
# changes or the grant is older than LEDGER_TTL_S.
LEDGER_DIR = "/var/log/panda"
LEDGER_NAME = "panda-EpicProdJobThrottler.ledger.json"
LEDGER_TTL_S = 180


class GrantLedger:
    """Jobs granted per site against a statistics reading, shared through a
    locked JSON file: ``{site: {"sig": [running, queued], "granted": n, "at": epoch}}``."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _load(self, fh) -> dict[str, dict[str, Any]]:
        fh.seek(0)
        raw = fh.read()
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _open(self):
        fh = open(self.path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def charge(self, readings: list[SiteReading]) -> None:
        """Set each reading's ``granted`` from the ledger: the grants made
        against this same reading within the TTL."""
        now = time.time()
        with self._open() as fh:
            data = self._load(fh)
        for r in readings:
            entry = data.get(r.site)
            if not entry:
                continue
            if entry.get("sig") == [r.running, r.queued] and now - float(entry.get("at", 0)) < LEDGER_TTL_S:
                r.granted = int(entry.get("granted", 0))

    def grant(self, readings: list[SiteReading], sites: list[str], n: int) -> None:
        """Charge ``n`` jobs to each of ``sites`` against its current reading."""
        now = time.time()
        by_site = {r.site: r for r in readings}
        with self._open() as fh:
            data = self._load(fh)
            for site in sites:
                r = by_site.get(site)
                if r is None:
                    continue
                entry = data.get(site) or {}
                sig = [r.running, r.queued]
                granted = int(entry.get("granted", 0)) if entry.get("sig") == sig and now - float(entry.get("at", 0)) < LEDGER_TTL_S else 0
                data[site] = {"sig": sig, "granted": granted + n, "at": now}
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(data))
            fh.flush()


def ledger_path() -> str:
    directory = LEDGER_DIR if os.access(LEDGER_DIR, os.W_OK) else tempfile.gettempdir()
    return os.path.join(directory, LEDGER_NAME)
