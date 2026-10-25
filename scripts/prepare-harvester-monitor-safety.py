#!/usr/bin/env python3
"""Prepare, without installing, a Harvester worker-monitor safety patch.

Read the exact installed pandaharvester package using --package. Write patched
copies, unified diffs and their input/output hashes under --output. The source
package is never modified. See docs/HARVESTER_WORKER_STATE.md.
"""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path


PATCHES = {
    'harvestermonitor/htcondor_monitor.py': [
        (
            'class HTCondorMonitor(PluginBase):\n',
            'class HTCondorMonitor(PluginBase):\n'
            '    # Scheduler uncertainty cannot authorize worker termination.\n'
            '    preserveUnknownWorkers = True\n\n',
        ),
        (
            '            except Exception as e:\n'
            '                host_job_ads_dict = {}\n'
            '                ret_err_str = f"Exception {e.__class__.__name__}: {e}"\n'
            '                tmpLog.error(ret_err_str)\n',
            '            except Exception as e:\n'
            '                ret_err_str = f"Condor query failed; worker states unchanged: {e.__class__.__name__}: {e}"\n'
            '                tmpLog.error(ret_err_str)\n'
            '                return False, ret_err_str\n',
        ),
    ],
    'harvesterbody/monitor.py': [
        (
            '                if not tmpStat:\n'
            '                    tmp_log.error(f"failed to check workers with: {tmpOut}")\n'
            '                    workersToCheck = []\n'
            '                    tmpOut = []\n',
            '                if not tmpStat:\n'
            '                    tmp_log.error(f"failed to check workers with: {tmpOut}")\n'
            '                    for unchecked in workersToCheck:\n'
            '                        retMap[unchecked.workerID].update(\n'
            '                            newStatus=unchecked.status, monStatus=unchecked.status,\n'
            '                            isChecked=False,\n'
            '                            workAttributes=unchecked.workAttributes or {},\n'
            '                            diagMessage=f"WORKER_STATE_UNVERIFIED: monitor query failed: {tmpOut}",\n'
            '                        )\n'
            '                    workersToCheck = []\n'
            '                    tmpOut = []\n',
        ),
        (
            '                            # kill due to timeout\n'
            '                            tmp_log.debug(f"kill workerID={workerID} due to consecutive check failures")\n'
            '                            self.dbProxy.mark_workers_to_kill_by_workerids([workSpec.workerID])\n'
            '                            newStatus = WorkSpec.ST_cancelled\n'
            '                            diagMessage = "Killed by Harvester due to consecutive worker check failures. " + diagMessage\n'
            '                            workSpec.set_pilot_error(PilotErrors.FAILEDBYSERVER, diagMessage)\n',
            '                            if (getattr(mon_core, "preserveUnknownWorkers", False)\n'
            '                                    and getattr(workSpec, "nativeStatus", None) == "unknown"):\n'
            '                                tmp_log.error(f"WORKER_STATE_UNVERIFIED workerID={workerID}: "\n'
            '                                              f"scheduler state unknown beyond checkTimeout; "\n'
            '                                              f"retaining {workSpec.status}; {diagMessage}")\n'
            '                                newStatus = workSpec.status\n'
            '                            else:\n'
            '                                # Existing policy for other monitor plugins.\n'
            '                                tmp_log.debug(f"kill workerID={workerID} due to consecutive check failures")\n'
            '                                self.dbProxy.mark_workers_to_kill_by_workerids([workSpec.workerID])\n'
            '                                newStatus = WorkSpec.ST_cancelled\n'
            '                                diagMessage = "Killed by Harvester due to consecutive worker check failures. " + diagMessage\n'
            '                                workSpec.set_pilot_error(PilotErrors.FAILEDBYSERVER, diagMessage)\n',
        ),
    ],
    'harvestermisc/htcondor_utils.py': [
        (
            '                errStr = f\'command "{comStr}" failed, retCode={retCode}, error: {stdOut} {stdErr}\'\n'
            '                tmpLog.error(errStr)\n',
            '                errStr = f\'command "{comStr}" failed, retCode={retCode}, error: {stdOut} {stdErr}\'\n'
            '                tmpLog.error(errStr)\n'
            '                raise RuntimeError(errStr)\n',
        ),
    ],
}


def prepare_sources(sources):
    result = {}
    for relative, changes in PATCHES.items():
        source = sources[relative]
        for old, new in changes:
            if new in source:
                continue
            if source.count(old) != 1:
                raise ValueError(f'Unsupported source context in {relative}')
            source = source.replace(old, new, 1)
        ast.parse(source, filename=relative)
        result[relative] = source
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    package, output = args.package.resolve(), args.output.resolve()
    if output == package or package in output.parents:
        parser.error('Output must be outside the source package')
    sources = {rel: (package / rel).read_text() for rel in PATCHES}
    prepared = prepare_sources(sources)
    # Check every input and patch context before writing any result.
    for relative in [*prepared, 'worker-monitor-safety.patch', 'manifest.json']:
        target = (output / relative).resolve()
        if target == package or package in target.parents:
            parser.error('Output symlinks must not point into the source package')
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'source_package': str(package), 'installed': False, 'files': {}}
    combined = []
    for relative, patched in prepared.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patched)
        combined.extend(difflib.unified_diff(
            sources[relative].splitlines(True), patched.splitlines(True),
            fromfile='a/pandaharvester/' + relative,
            tofile='b/pandaharvester/' + relative))
        manifest['files'][relative] = {
            'before_sha256': hashlib.sha256(sources[relative].encode()).hexdigest(),
            'after_sha256': hashlib.sha256(patched.encode()).hexdigest(),
        }
    (output / 'worker-monitor-safety.patch').write_text(''.join(combined))
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
