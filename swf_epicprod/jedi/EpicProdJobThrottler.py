"""The ePIC job throttler for JEDI (docs/EPIC_JOB_THROTTLER.md).

The JEDI-facing engine: a ``JobThrottlerBase`` subclass that reads the
per-site job statistics and the site-keyed limits through the task
buffer, takes the decision of ``epic_job_throttler.decide``, logs it,
and in ``throttle`` mode returns it to the job generator. In
explicit ``observe`` mode, it logs what it would do and answers
unthrottled. Missing or invalid modes and absent enforcement prerequisites
hold generation with a temporary error.

Registration, in ``panda_jedi.cfg`` ``[jobthrottle]``::

    modConfig = epic:any:swf_epicprod.jedi.EpicProdJobThrottler:EpicProdJobThrottler

Configuration rows in the ``config`` table, component
``epic_job_throttler``, app ``jedi``, VO ``epic``: ``MODE`` (``observe``
or ``throttle``) and, per site as ``<TAG>_<site>`` or work-queue-wide
as ``<TAG>``: ``THROTTLE_THRESHOLD``, ``NQUEUELIMIT``, ``NRUNNINGCAP``,
``NQUEUECAP``. The engine's log is ``panda-EpicProdJobThrottler.log``.

The saturated sites are exposed as ``excluded_sites`` for the job
generator to keep out of task selection. Whether the installed
generator honors them is read from the task buffer's
``getTasksToBeProcessed_JEDI`` signature; without it a saturated site
throttles the work queue (the decision's ``exclusion_honored``).

The statistics are pre-cached tables refreshed about once a minute
while passes come every second or so, so the engine keeps a ledger of
the jobs granted against each site's reading, shared by the generator
processes through a file, and charges them to the reading until it
changes or the grants age out.
"""

from __future__ import annotations

import inspect
import threading
import time
from typing import Any

from pandacommon.pandalogger.PandaLogger import PandaLogger
from pandajedi.jedicore.JediTaskBuffer import JediTaskBuffer
from pandajedi.jediconfig import jedi_config
from pandajedi.jedicore.MsgWrapper import MsgWrapper
from pandajedi.jedithrottle.JobThrottlerBase import JobThrottlerBase

from swf_epicprod.jedi.epic_job_throttler import (
    CONFIG_TAGS, LEDGER_TTL_S, GrantLedger, combined_statistics, decide,
    generation_scope, ledger_path, parse_pools, policy_value, readings_from_stats,
)

logger = PandaLogger().getLogger(__name__.split(".")[-1])

COMPONENT = "epic_job_throttler"
APP = "jedi"
MODE_OBSERVE = "observe"
MODE_THROTTLE = "throttle"
# Each configuration value read, a missing one included, is kept this long
# in the process. A pass reads up to four keys per limit per site (two VOs,
# site and wide keys) and passes come every second or so; read through
# without keeping them, they were millions of task-buffer calls a day, and
# JEDI recycles a task-buffer process after every thousand calls, leaking
# two pipes in the caller each time (pandajedi Interaction). On 2026-10-30
# a generator process reached its 1,024 descriptors at 20:42 ET and JEDI
# stopped until a restart at 02:01 ET (distcomp-services
# panda-server/CHANGES.md). A changed setting takes effect within this time.
CONFIG_TTL_S = 60

# The installed generator passes the saturated sites to task selection
# only when its task buffer takes them.
EXCLUSION_HONORED = "excluded_sites" in inspect.signature(JediTaskBuffer.getTasksToBeProcessed_JEDI).parameters

class EpicProdJobThrottler(JobThrottlerBase):
    """Per-site pacing of job generation for the epic VO."""

    def __init__(self, taskBufferIF) -> None:
        JobThrottlerBase.__init__(self, taskBufferIF)
        self.comp_name = COMPONENT
        self.app = APP
        self.excluded_sites: list[str] = []
        self._config_kept: dict[tuple[str, str], tuple[Any, float]] = {}
        self._config_lock = threading.Lock()
        self.ledger = GrantLedger(ledger_path())
        MsgWrapper(logger).info(f"engine up: exclusion_honored={EXCLUSION_HONORED} ledger={self.ledger.path} ttl={LEDGER_TTL_S}s")

    def refresh(self) -> None:
        JobThrottlerBase.refresh(self)
        self.excluded_sites = []

    def _config_value(self, vo: str, key: str) -> Any:
        return policy_value(self._raw_config_value, vo, key)

    def _raw_config_value(self, vo: str, key: str) -> Any:
        """One configuration value, read through the task buffer at most
        once per ``CONFIG_TTL_S`` in this process."""
        now = time.monotonic()
        with self._config_lock:
            kept = self._config_kept.get((vo, key))
            if kept is not None and now - kept[1] < CONFIG_TTL_S:
                return kept[0]
        value = self.taskBufferIF.getConfigValue(self.comp_name, key, self.app, vo)
        with self._config_lock:
            self._config_kept[(vo, key)] = (value, now)
        return value

    def _mode(self, vo: str) -> str:
        mode = self._config_value(vo, "MODE")
        return str(mode or '').strip().lower()

    @staticmethod
    def _lock_enabled(vo, label, queue, cloud):
        for rule in str(getattr(jedi_config.jobgen, 'lockProcess', '')).split(','):
            fields = rule.split(':')
            if len(fields) == 4 and all(
                    wanted in ('', 'any') or str(actual) in wanted.split('|')
                    for wanted, actual in zip(fields, (vo, label, queue, cloud))):
                return True
        return False

    def _site_config(self, vo: str, sites: list[str]) -> dict[str, dict[str, Any]]:
        """Per-site limits: ``<TAG>_<site>``, else the work-queue-wide
        ``<TAG>``; a tag with neither is left to the decision's default."""
        out: dict[str, dict[str, Any]] = {}
        for site in sites:
            cfg: dict[str, Any] = {}
            for tag in CONFIG_TAGS:
                value = policy_value(self._raw_config_value, vo, tag, site)
                if value is not None:
                    cfg[tag] = value
            if cfg:
                out[site] = cfg
        return out

    def toBeThrottled(self, vo, prodSourceLabel, cloudName, workQueue, resource_name):
        self.refresh()
        tmp_log = MsgWrapper(logger)
        header = f"{vo}:{prodSourceLabel} cloud={cloudName} queue={workQueue.queue_name} resource_type={resource_name}:"
        tmp_log.debug(f"{header} start")

        try:
            stats = combined_statistics(self.taskBufferIF, workQueue)
        except (ValueError, TypeError) as exc:
            tmp_log.error(f"{header} failed to get combined per-site job statistics: {exc}")
            return self.retTmpError

        # Every site with jobs of any resource type is read: a site's
        # queue is one pool, and a pass of another resource type must see
        # it saturated too.
        sites = sorted(stats)
        # Queues that feed one set of slots are read as one pool, held to
        # the pool's limits (``POOLS``, ``<TAG>_<pool>``).
        pools = parse_pools(self._config_value(vo, "POOLS"))
        readings = readings_from_stats(
            stats, resource_name, self._site_config(vo, sites + sorted(pools)), pools)
        mode = self._mode(vo)
        if mode not in (MODE_OBSERVE, MODE_THROTTLE):
            tmp_log.error(f'{header} invalid or missing MODE; holding generation')
            return self.retTmpError
        if mode == MODE_THROTTLE:
            if not EXCLUSION_HONORED or not self._lock_enabled(vo, prodSourceLabel, workQueue.queue_name, cloudName):
                tmp_log.error(f'{header} regulation prerequisites absent; holding generation')
                return self.retTmpError
            try:
                decision = self.ledger.reserve(readings, scope=generation_scope(workQueue),
                                               exclusion_honored=EXCLUSION_HONORED)
            except (OSError, ValueError, TypeError) as e:
                tmp_log.error(f'{header} grant ledger unavailable; holding generation: {e}')
                return self.retTmpError
        else:
            decision = decide(readings, exclusion_honored=EXCLUSION_HONORED)

        for line in decision.lines:
            tmp_log.info(f"{header} {line}")

        if mode != MODE_THROTTLE:
            tmp_log.info(
                f"{header} OBSERVE would {'THROTTLE' if decision.throttled else 'PASS'}"
                f" max_num_jobs={decision.max_num_jobs} excluded={decision.excluded_sites}; PASS unthrottled"
            )
            return self.retUnThrottled

        self.excluded_sites = list(decision.excluded_sites)
        tmp_log.info(f'REGULATION_OK vo={vo} mode=throttle lock=true exclusions=true '
                     f'accounting=epic+wlcg scope={generation_scope(workQueue)} '
                     f'decision={"SKIP" if decision.throttled else "PASS"}')
        if decision.throttled:
            tmp_log.info(f"{header} SKIP throttled: excluded {self.excluded_sites}")
            return self.retThrottled
        if decision.max_num_jobs is not None:
            self.setMaxNumJobs(decision.max_num_jobs)
        tmp_log.info(f"{header} PASS max_num_jobs={self.maxNumJobs} excluded={self.excluded_sites} charged={decision.granted_sites}")
        return self.retUnThrottled
