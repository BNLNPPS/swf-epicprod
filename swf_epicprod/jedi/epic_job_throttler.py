"""The ePIC job throttler's decision (docs/EPIC_JOB_THROTTLER.md).

A pure function of the per-site job statistics and the configuration,
with no PanDA dependency, so it is tested without a server. The
JEDI-facing engine that gathers the inputs and returns the answer to
the job generator is ``EpicProdJobThrottler`` in this package.

ePIC tasks are pinned to one site each, so the ATLAS engine's count
over a whole work queue puts a saturated site and a starved one in the
same group; this decision reads the same statistics per site and
applies the ATLAS rule to each: a site is saturated when its queued
jobs (assigned, activated, defined; starting counts as running) exceed the larger of
``THROTTLE_THRESHOLD`` times its running jobs and ``NQUEUELIMIT``, or
when a cap on running or queued jobs is exceeded. The work queue is
throttled when every site is saturated; otherwise the pass is capped at
the room of the unsaturated sites and the saturated sites are named
for exclusion from task selection. A job generator without site
exclusion (the installed server as of 2026-09-17) generates for the
saturated site's tasks on any unthrottled answer; a saturated site
never holds the whole work queue for that, nor for being the only site
with jobs (``HOLD_QUEUE_ON_SATURATED_SITE``, off since 2026-10-05).

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
# A starting job is on a slot: a job is recorded running late and
# works for most of its life as starting
# (2026-10-05, median of the last 200 finished: 87 of 115 min at
# BNL_OSG_EPIC_PROD_1, 4.9 of 5.1 min at UM_GREX_PanDA_1, which read 0
# running). Counted as running, not queued, unlike the ATLAS engine.
RUNNING_STATES = ("running", "starting")
NOT_RUN_STATES = ("assigned", "activated")
CONFIG_TAGS = ("THROTTLE_THRESHOLD", "NQUEUELIMIT", "NRUNNINGCAP", "NQUEUECAP")
COMPATIBLE_VOS = ("epic", "wlcg")


def policy_value(fetch, vo, key, site=None):
    """Use epic policy for both names, with legacy values as fallback.

    A canonical wide setting precedes a legacy site override, so tuning
    epic alone cannot leave a different policy on the draining VO.
    """
    voices = COMPATIBLE_VOS if vo in COMPATIBLE_VOS else (vo,)
    keys = (f"{key}_{site}", key) if site is not None else (key,)
    for voice in voices:
        for name in keys:
            value = fetch(voice, name)
            if value is not None:
                return value
    return None


def generation_scope(workqueue):
    """Equivalent queues share grants across VO names and resource types."""
    if workqueue.VO not in COMPATIBLE_VOS:
        return f"{workqueue.VO}:{workqueue.queue_id}"
    kind = 'share' if workqueue.is_global_share else 'queue'
    return f"epic:{kind}:{workqueue.queue_type}:{workqueue.queue_name}"


def combined_statistics(task_buffer, workqueue):
    """Sum equivalent work queues under both VO names, without mixing labels.

    Missing peer queues or failed reads refuse generation: neither is
    evidence that the legacy workload has disappeared.
    """
    queues = [workqueue]
    if workqueue.VO in COMPATIBLE_VOS:
        mapper = task_buffer.getWorkQueueMap()
        for voice in COMPATIBLE_VOS:
            if voice == workqueue.VO:
                continue
            peer = mapper.getQueueByName(voice, workqueue.queue_type,
                                        workqueue.queue_name) if mapper else None
            if peer is None or peer.is_global_share != workqueue.is_global_share:
                raise ValueError(f'missing equivalent {voice} work queue {workqueue.queue_name}')
            queues.append(peer)
    merged = {}
    for queue in queues:
        ok, stats = task_buffer.getJobStatisticsByResourceTypeSite(queue)
        if not ok:
            raise ValueError(f'statistics unavailable for {queue.VO}:{queue.queue_name}')
        for site, resources in stats.items():
            for resource, statuses in resources.items():
                target = merged.setdefault(site, {}).setdefault(resource, {})
                for status, count in statuses.items():
                    target[status] = target.get(status, 0) + int(count)
    return merged
# The reading a pass is charged to while no site has jobs at all: a pass
# with nothing queued anywhere is not unbounded, it is bounded by the
# default floor and the ledger like any site (2026-09-18: the MCORE
# passes read no site and passed uncapped while the SCORE passes held
# NERSC_Perlmutter_epic saturated; 140,000 jobs generated in 30 minutes).
NO_SITE = "(no site has jobs)"
# A saturated site holding the whole work queue: off (Torre, 2026-10-05).
# With it on, UM_GREX_PanDA_1 saturated held every production pass at
# every site from at least 2026-09-26 to 2026-10-05. Off, a pass the rule
# would have held goes on, capped: at the room of the open sites or, with
# none open, at the floor of the NO_SITE reading, which stands for the
# sites with no jobs yet (a new task at a quiet site) and is charged in
# the ledger like a site.
HOLD_QUEUE_ON_SATURATED_SITE = False


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
    # the PanDA queues a pool reading stands for; empty for a single site
    members: tuple[str, ...] = ()

    @property
    def sites(self) -> list[str]:
        """The PanDA queues this reading excludes or grants together."""
        return list(self.members) if self.members else [self.site]

    @property
    def label(self) -> str:
        return f"{self.site} ({'+'.join(self.members)})" if self.members else self.site

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

    A site exactly at its bound (room 0, not over it) is excluded with
    the saturated ones: another site's room must never be spent on its
    tasks. On 2026-10-21 GREX read room 0 (queued 2,814 of 2,814), was
    left off the list, and the generator made 4,999 jobs for each of two
    GREX tasks.
    """
    lines: list[str] = []
    saturated: list[str] = []
    full: list[str] = []
    open_sites: list[str] = []
    room = 0
    # The NO_SITE reading beside real sites is the spare for the sites with
    # no jobs yet, used only when the hold-all rule is off and no real site
    # is open; alone, it is read as a site.
    spare = next((r for r in readings if r.site == NO_SITE), None) if len(readings) > 1 else None
    readings = [r for r in readings if r is not spare]
    for r in sorted(readings, key=lambda x: x.site):
        why = r.saturation()
        if why:
            saturated.extend(r.sites)
            lines.append(f"{r.label}: SATURATED {why}; running={r.running} queued={r.queued} granted={r.granted}")
        elif r.room <= 0:
            full.extend(r.sites)
            lines.append(f"{r.label}: FULL room 0 (bound {r.bound:.0f}, queued {r.queued}, granted {r.granted}, running {r.running})")
        else:
            open_sites.append(r.site)
            room += r.room
            lines.append(f"{r.label}: room {r.room} (bound {r.bound:.0f}, queued {r.queued}, granted {r.granted}, running {r.running})")
    excluded = saturated + full
    if not readings:
        # Unreachable when the caller charges the no-site reading; kept as
        # the bounded answer for a caller that does not.
        return Decision(False, PASS_MAX_JOBS, [], [NO_SITE], ["no site has jobs: one capped pass"])
    if HOLD_QUEUE_ON_SATURATED_SITE:
        if len(saturated) == len(readings):
            return Decision(True, None, saturated, [], lines + ["every site saturated: THROTTLED"])
        if saturated and not exclusion_honored:
            return Decision(True, None, saturated, [], lines + [f"saturated {saturated} and the generator has no site exclusion: THROTTLED"])
    elif excluded and room <= 0 and spare is not None and spare.room > 0:
        max_num_jobs = min(spare.room, PASS_MAX_JOBS)
        lines.append(f"excluded {excluded}, no open site: unthrottled for sites with no jobs: max_num_jobs={max_num_jobs} (spare room {spare.room}, granted {spare.granted})")
        return Decision(False, max_num_jobs, excluded, [NO_SITE], lines)
    max_num_jobs = min(room, PASS_MAX_JOBS)
    if max_num_jobs <= 0:
        return Decision(True, None, excluded, [], lines + ["no room at any open site: THROTTLED"])
    lines.append(f"unthrottled: max_num_jobs={max_num_jobs} excluded={excluded or '[]'}")
    return Decision(False, max_num_jobs, excluded, open_sites, lines)


def parse_pools(value: Any) -> dict[str, list[str]]:
    """The ``POOLS`` configuration value, ``pool:queueA,queueB;pool2:...``,
    as ``{pool: [queue, ...]}``; anything unreadable is no pool."""
    pools: dict[str, list[str]] = {}
    for item in str(value or "").split(";"):
        name, _, members = item.partition(":")
        queues = [m.strip() for m in members.split(",") if m.strip()]
        if name.strip() and queues:
            pools[name.strip()] = queues
    return pools


def readings_from_stats(
    stats: dict[str, dict[str, dict[str, int]]],
    resource_name: str,
    site_config: dict[str, dict[str, Any]],
    pools: dict[str, list[str]] | None = None,
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

    A pool (``pools``, from the ``POOLS`` configuration) is several PanDA
    queues feeding one set of slots: the OSG pool behind BNL_OSG_PanDA_1
    and BNL_OSG_EPIC_PROD_1. It is one reading over its queues' summed
    counts, held to the pool's own limits (``<TAG>_<pool>``), and its
    queues are excluded or granted together, whether or not they have
    jobs yet. Read per queue, each queue had its own floor and its own
    share of the running count, and the pool's allowance was the sum.
    """
    def counts(site):
        running = not_run = defined = 0
        for by_status in (stats.get(site) or {}).values():
            running += sum(int(by_status.get(s, 0) or 0) for s in RUNNING_STATES)
            not_run += sum(int(by_status.get(s, 0) or 0) for s in NOT_RUN_STATES)
            defined += int(by_status.get("defined", 0) or 0)
        return running, not_run, defined

    def reading(name, running, not_run, defined, members=()):
        cfg = site_config.get(name, {})
        return SiteReading(
            site=name,
            running=running,
            not_run=not_run,
            defined=defined,
            threshold=float(cfg.get("THROTTLE_THRESHOLD", DEFAULT_THRESHOLD)),
            nqueuelimit=int(cfg.get("NQUEUELIMIT", DEFAULT_NQUEUELIMIT)),
            nrunningcap=cfg.get("NRUNNINGCAP"),
            nqueuecap=cfg.get("NQUEUECAP"),
            members=tuple(members),
        )

    readings: list[SiteReading] = []
    pooled: set[str] = set()
    for pool, members in sorted((pools or {}).items()):
        members = sorted(members)
        pooled.update(members)
        if not any(m in stats for m in members):
            continue  # a quiet pool is served like any quiet site
        totals = [sum(c) for c in zip(*(counts(m) for m in members))]
        readings.append(reading(pool, *totals, members=members))
    for site in sorted(stats):
        if site not in pooled:
            readings.append(reading(site, *counts(site)))
    # With no site at all it is the reading; beside sites it is the spare
    # for the sites with no jobs yet (HOLD_QUEUE_ON_SATURATED_SITE).
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
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('grant ledger is not an object')
        if any(not isinstance(entry, dict) for entry in data.values()):
            raise ValueError('grant ledger has an invalid entry')
        return data

    def _open(self):
        fh = open(self.path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def reserve(self, readings, *, scope, exclusion_honored):
        """Read, decide and reserve under one lock across generator processes.

        Statistics are work-queue scoped; resource types share that scope.
        A corrupt or unwritable ledger raises rather than losing reservations.
        """
        now = time.time()
        # Lock a stable separate inode, so the ledger itself can be replaced
        # atomically. A crash must not leave an empty file that resets grants.
        with open(self.path + '.lock', 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with open(self.path) as fh:
                    if not fh.read(1):
                        raise ValueError('grant ledger is empty')
                    data = self._load(fh)
            except FileNotFoundError:
                data = {}
            for r in readings:
                entry = data.get(f'{scope}/{r.site}', {})
                if (entry.get('sig') == [r.running, r.queued]
                        and now - float(entry.get('at', 0)) < LEDGER_TTL_S):
                    r.granted = int(entry.get('granted', 0))
            decision = decide(readings, exclusion_honored=exclusion_honored)
            if not decision.throttled and decision.max_num_jobs:
                for r in readings:
                    if r.site in decision.granted_sites:
                        data[f'{scope}/{r.site}'] = {
                            'sig': [r.running, r.queued],
                            'granted': r.granted + decision.max_num_jobs, 'at': now}
                fd, temporary = tempfile.mkstemp(dir=os.path.dirname(self.path) or '.')
                try:
                    with os.fdopen(fd, 'w') as fh:
                        fh.write(json.dumps(data))
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return decision

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
