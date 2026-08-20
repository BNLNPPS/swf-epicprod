"""Submit the coprocessor driver as a direct PanDA job (no JEDI) to BNL_NPPS_GPU.

The task path rejects the object-store log destination at the refiner
("unknown endpoint: local", task 39012); the direct path sets it freely and
its gates are recorded and verified in tools/npps0/submit_direct_job.py,
whose job spec this clones: prodSourceLabel test, processingType
gangarobot-* (keeps the original dataset name), destinationSE local (Adder
skips Rucio registration; the log goes to S3 by the queue's s3 copytool).
Log-only job: the driver's verdict is its exit code and the COPROC-DRIVER
line plus job_summary.json content in the log.

Run on npps0 (X509_CERT_DIR must point at a CA directory carrying the
IGTF chain — see the capath note below):

    source ~/.env
    export PANDA_AUTH=oidc PANDA_AUTH_VO=EIC.production PANDA_CONFIG_ROOT=$HOME/.pathena
    export PANDA_URL_SSL=https://pandaserver01.sdcc.bnl.gov:25443/server/panda
    export PANDA_URL=http://pandaserver01.sdcc.bnl.gov:25080/server/panda
    export X509_CERT_DIR=$HOME/work/certs/capath   # hashed dir with the IGTF chain
    PYTHONPATH=$HOME/github/panda-client:$HOME/github/panda-server \
        python3 submit_coprocessor_job.py [--version d01] [--units 3] [--count 100000]
"""

import argparse
import json
import sys
import time
import urllib.parse

TASK_BASE = 'group.EIC.npps0.coproc.test'
QUEUE = 'BNL_NPPS_GPU'
SOURCE_URL = 'https://pandaserver01.sdcc.bnl.gov:25443'
TRF = 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02'
CONTAINER = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly'

PREFIX = '/home/wenaus/work/simphony-synrad-install'
REFSET = '/home/wenaus/work/synrad-refset-20260820-release'


def payload(args, job_name):
    return ('nvidia-smi -L && '
            'git clone --depth 1 https://github.com/BNLNPPS/swf-epicprod.git && '
            'python3 swf-epicprod/tools/worker/coprocessor/driver.py'
            f' --units {args.units} --count {args.count}'
            f' --task-name {job_name} --executable oneshot --exec-mode direct'
            f' --prefix {PREFIX}'
            f' --geom synrad --geom-cfbase {REFSET}/geometry'
            f' --geom-edition synrad_bench-v1'
            f' --refset-input {REFSET}/inphoton/synrad_service_inphoton.npy && '
            'echo "=== job_summary.json ===" && cat job_summary.json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', default='d01')
    ap.add_argument('--units', type=int, default=3)
    ap.add_argument('--count', type=int, default=100000)
    ap.add_argument('--log-ds', default='',
                    help='existing log dataset to reuse (the Setupper resolves a '
                         'pre-created dataset by lookup and fails on an unknown one)')
    args = ap.parse_args()

    from pandaserver.taskbuffer.JobSpec import JobSpec
    from pandaserver.taskbuffer.FileSpec import FileSpec
    from pandaclient import Client

    stamp = time.strftime('%Y%m%d%H%M%S')
    job_name = f'{TASK_BASE}.{args.version}.{stamp}'
    log_ds = args.log_ds or f'{TASK_BASE}.{args.version}_log'

    quoted = urllib.parse.quote(payload(args, job_name), safe='')
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
    job.prodSourceLabel = 'test'
    job.processingType = 'gangarobot-gpu'
    job.currentPriority = 1000
    job.coreCount = 8
    job.minRamCount = 3686
    job.maxDiskCount = 4396
    job.container_name = CONTAINER
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
