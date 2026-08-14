"""Submit the BNL_NPPS_GPU smoke-test task: raindrop on the 4090s under PanDA.

One noInput/noOutput job pinned to the GPU queue, running the Simphony
raindrop GPU test inside the locally staged eic_dev_cuda image. The task
parameter map mirrors the working epicproduction shape (taken from a
live task's stored jedi_taskparams): vo=epic, taskType=anal,
prodSourceLabel=test, runGen with a pseudo_input sequence number, and
multiStepExec containerOptions for in-container execution.

Run on the production host with the operator token:

    source ~/.env
    export PANDA_AUTH=oidc PANDA_AUTH_VO=EIC.production PANDA_CONFIG_ROOT=$HOME/.pathena
    export PANDA_URL_SSL=https://pandaserver01.sdcc.bnl.gov:25443/server/panda
    export PANDA_URL=http://pandaserver01.sdcc.bnl.gov:25080/server/panda
    PYTHONPATH=/data/wenauseic/github/panda-client \
        python tools/npps0/submit_test_task.py [--version vNN]
"""

import argparse
import sys
import urllib.parse

TASK_BASE = 'group.EIC.npps0.raindrop.test'
QUEUE = 'BNL_NPPS_GPU'
CONTAINER = '/home/wenaus/images/eic_dev_cuda_nightly.sif'
SOURCE_URL = 'https://pandaserver01.sdcc.bnl.gov:25443'
PAYLOAD = ('nvidia-smi -L && '
           'git clone --depth 1 https://github.com/BNLNPPS/simphony.git && '
           'cd simphony/dd4hepplugins/examples && '
           'python3 test_raindrop_dd4hep_gpu.py')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', default='v03')
    args = ap.parse_args()

    task_name = f'{TASK_BASE}.{args.version}'
    params = {
        'taskName': task_name,
        'userName': 'wenaus',
        'vo': 'epic',
        'workingGroup': 'EIC',
        'prodSourceLabel': 'test',
        'taskType': 'anal',
        'processingType': 'gputest',
        'taskPriority': 1000,
        'transPath': 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
        'transUses': '',
        'transHome': None,
        'architecture': '',
        'container_name': CONTAINER,
        'multiStepExec': {
            'preprocess': {'command': '${TRF}', 'args': '--preprocess ${TRF_ARGS}'},
            'postprocess': {'command': '${TRF}', 'args': '--postprocess ${TRF_ARGS}'},
            'containerOptions': {
                'containerExec': ('echo "=== cat exec script ==="; '
                                  'cat __run_main_exec.sh; echo; '
                                  'echo "=== exec script ==="; '
                                  '/bin/sh __run_main_exec.sh'),
                'containerImage': CONTAINER,
            },
        },
        'noInput': True,
        'noOutput': True,
        'nEvents': 1,
        'nEventsPerJob': 1,
        'coreCount': 8,
        'ramCount': 4096,
        'walltime': 3600,
        'maxAttempt': 3,
        'workDiskCount': 4096,
        'workDiskUnit': 'MB',
        'skipScout': True,
        'messageDriven': True,
        'pushStatusChanges': True,
        'cloudAsVO': True,
        'sourceURL': SOURCE_URL,
        'site': QUEUE,
        'cloud': 'EIC',
        'log': {
            'type': 'template',
            'param_type': 'log',
            'value': '${LOG0}',
            'dataset': f'{task_name}_log/',
            'hidden': True,
        },
        'jobParameters': [
            {'type': 'constant',
             'value': f'-j "" --sourceURL {SOURCE_URL}'},
            {'type': 'constant', 'value': '-r .'},
            {'type': 'template', 'param_type': 'pseudo_input',
             'value': '${SEQNUMBER}', 'dataset': 'seq_number',
             'offset': '0', 'hidden': True, 'expandedList': ['seq_number']},
            {'type': 'constant', 'value': '-p "', 'padding': False},
            {'type': 'constant',
             'value': urllib.parse.quote(PAYLOAD, safe='')},
            {'type': 'constant', 'value': '"'},
        ],
    }

    from pandaclient import Client
    status, result = Client.insertTaskParams(params)
    print('client status:', status)
    print('server result:', result)
    if status != 0:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
