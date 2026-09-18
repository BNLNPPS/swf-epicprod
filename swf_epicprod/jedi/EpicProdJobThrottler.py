"""The ePIC job throttler for JEDI (docs/EPIC_JOB_THROTTLER.md).

The JEDI-facing engine: a ``JobThrottlerBase`` subclass that reads the
per-site job statistics and the site-keyed limits through the task
buffer, takes the decision of ``epic_job_throttler.decide``, logs it,
and in ``throttle`` mode returns it to the job generator. In
``observe`` mode, the default, it logs what it would do and answers
unthrottled, so the server behaves as it did before the engine was
registered.

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
from typing import Any

from pandacommon.pandalogger.PandaLogger import PandaLogger
from pandajedi.jedicore.JediTaskBuffer import JediTaskBuffer
from pandajedi.jedicore.MsgWrapper import MsgWrapper
from pandajedi.jedithrottle.JobThrottlerBase import JobThrottlerBase

from swf_epicprod.jedi.epic_job_throttler import CONFIG_TAGS, LEDGER_TTL_S, GrantLedger, decide, ledger_path, readings_from_stats

logger = PandaLogger().getLogger(__name__.split(".")[-1])

COMPONENT = "epic_job_throttler"
APP = "jedi"
MODE_OBSERVE = "observe"
MODE_THROTTLE = "throttle"

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
        self.ledger = GrantLedger(ledger_path())
        MsgWrapper(logger).info(f"engine up: exclusion_honored={EXCLUSION_HONORED} ledger={self.ledger.path} ttl={LEDGER_TTL_S}s")

    def refresh(self) -> None:
        JobThrottlerBase.refresh(self)
        self.excluded_sites = []

    def _config_value(self, vo: str, key: str) -> Any:
        return self.taskBufferIF.getConfigValue(self.comp_name, key, self.app, vo)

    def _mode(self, vo: str) -> str:
        mode = self._config_value(vo, "MODE")
        return MODE_THROTTLE if str(mode or "").strip().lower() == MODE_THROTTLE else MODE_OBSERVE

    def _site_config(self, vo: str, sites: list[str]) -> dict[str, dict[str, Any]]:
        """Per-site limits: ``<TAG>_<site>``, else the work-queue-wide
        ``<TAG>``; a tag with neither is left to the decision's default."""
        wide = {tag: self._config_value(vo, tag) for tag in CONFIG_TAGS}
        out: dict[str, dict[str, Any]] = {}
        for site in sites:
            cfg: dict[str, Any] = {}
            for tag in CONFIG_TAGS:
                value = self._config_value(vo, f"{tag}_{site}")
                if value is None:
                    value = wide[tag]
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

        ok, stats = self.taskBufferIF.getJobStatisticsByResourceTypeSite(workQueue)
        if not ok:
            tmp_log.error(f"{header} failed to get per-site job statistics")
            return self.retTmpError

        # Every site with jobs of any resource type is read: a site's
        # queue is one pool, and a pass of another resource type must see
        # it saturated too.
        sites = sorted(stats)
        readings = readings_from_stats(stats, resource_name, self._site_config(vo, sites))
        try:
            self.ledger.charge(readings)
        except OSError as e:
            tmp_log.error(f"{header} ledger read failed ({self.ledger.path}): {e}; deciding on the reading alone")
        decision = decide(readings, exclusion_honored=EXCLUSION_HONORED)
        mode = self._mode(vo)

        for line in decision.lines:
            tmp_log.info(f"{header} {line}")

        if mode != MODE_THROTTLE:
            tmp_log.info(
                f"{header} OBSERVE would {'THROTTLE' if decision.throttled else 'PASS'}"
                f" max_num_jobs={decision.max_num_jobs} excluded={decision.excluded_sites}; PASS unthrottled"
            )
            return self.retUnThrottled

        self.excluded_sites = list(decision.excluded_sites)
        if decision.throttled:
            tmp_log.info(f"{header} SKIP throttled: saturated {self.excluded_sites}")
            return self.retThrottled
        if decision.max_num_jobs is not None:
            self.setMaxNumJobs(decision.max_num_jobs)
            try:
                self.ledger.grant(readings, decision.granted_sites, decision.max_num_jobs)
            except OSError as e:
                tmp_log.error(f"{header} ledger write failed ({self.ledger.path}): {e}")
        tmp_log.info(f"{header} PASS max_num_jobs={self.maxNumJobs} excluded={self.excluded_sites} charged={decision.granted_sites}")
        return self.retUnThrottled
