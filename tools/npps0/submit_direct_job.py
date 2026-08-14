"""Submit the raindrop smoke-test as a direct PanDA job (no JEDI).

The task path cannot reach destinationSE='local' — the task refiner
validates any log destination as a writable Rucio RSE — while the
Adder's registration skip keys on exactly that value. Direct
submission (the classic submitJobs API) sets it freely on the job.
The job spec is a literal clone of a task job that ran to completion
(payload PASS, log uploaded to S3): same transformation, parameter
string, container block, and queue; only the log's destinationSE
differs, so the server-side Rucio registration that failed v09
(ddmErrorCode 200) is skipped.

Run on the production host with the operator token:

    source ~/.env
    export PANDA_AUTH=oidc PANDA_AUTH_VO=EIC.production PANDA_CONFIG_ROOT=$HOME/.pathena
    export PANDA_URL_SSL=https://pandaserver01.sdcc.bnl.gov:25443/server/panda
    export PANDA_URL=http://pandaserver01.sdcc.bnl.gov:25080/server/panda
    PYTHONPATH=/data/wenauseic/github/panda-client:/data/wenauseic/github/panda-server \
        python tools/npps0/submit_direct_job.py [--version d01]
"""

import argparse
import json
import sys
import time
import urllib.parse

TASK_BASE = 'group.EIC.npps0.raindrop.test'
QUEUE = 'BNL_NPPS_GPU'
SOURCE_URL = 'https://pandaserver01.sdcc.bnl.gov:25443'
TRF = 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02'
CONTAINER = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly'
PAYLOAD = ('nvidia-smi -L && '
           'git clone --depth 1 https://github.com/BNLNPPS/simphony.git && '
           'cd simphony/dd4hepplugins/examples && '
           'python3 test_raindrop_dd4hep_gpu.py')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', default='d01')
    args = ap.parse_args()

    from pandaserver.taskbuffer.JobSpec import JobSpec
    from pandaserver.taskbuffer.FileSpec import FileSpec
    from pandaclient import Client

    stamp = time.strftime('%Y%m%d%H%M%S')
    job_name = f'{TASK_BASE}.{args.version}.{stamp}'
    log_ds = f'{TASK_BASE}.{args.version}_log'

    quoted = urllib.parse.quote(PAYLOAD, safe='')
    base_args = f'-j "" --sourceURL {SOURCE_URL} -r . -p "{quoted} "'
    msexec_dict = {
        'preprocess': {'command': TRF, 'args': f'--preprocess {base_args}'},
        'postprocess': {'command': TRF, 'args': f'--postprocess {base_args}'},
        'containerOptions': {
            'containerExec': ('echo "=== cat exec script ==="; '
                              'cat __run_main_exec.sh; echo; '
                              'echo "=== exec script ==="; '
                              '/bin/sh __run_main_exec.sh'),
            'containerImage': CONTAINER,
        },
    }
    msexec = f'<MULTI_STEP_EXEC>{json.dumps(msexec_dict)}</MULTI_STEP_EXEC>'

    job = JobSpec()
    job.jobDefinitionID = 1
    job.jobName = job_name
    job.transformation = TRF
    job.destinationDBlock = log_ds
    job.destinationSE = 'local'
    job.computingSite = QUEUE
    job.cloud = 'EIC'
    job.VO = 'epic'
    job.workingGroup = 'EIC'
    # ptest + prun: the Setupper keeps the original dataset name for
    # this combination (no _sub dataset), and with destinationSE
    # 'local' it skips registration and resolves the pre-created
    # dataset by lookup. ptest is a neutral source: dispatched as
    # production job type on production queues.
    job.prodSourceLabel = 'ptest'
    job.processingType = 'prun'
    job.currentPriority = 1000
    job.coreCount = 8
    job.minRamCount = 3686
    job.maxDiskCount = 4396
    job.jobParameters = f'{base_args}{msexec}'

    log = FileSpec()
    log.lfn = f'{job_name}.log.tgz'
    log.type = 'log'
    log.scope = 'group.EIC'
    log.dataset = log_ds
    log.destinationDBlock = log_ds
    log.destinationSE = 'local'
    job.addFile(log)

    status, result = Client.submitJobs([job])
    print('client status:', status)
    print('server result:', result)
    return 0 if status == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
