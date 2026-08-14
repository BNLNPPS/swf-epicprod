"""Submit the BNL_NPPS_GPU smoke-test task: raindrop on the 4090s under PanDA.

One noInput job, pinned to the GPU queue, running the Simphony raindrop
GPU test inside the locally staged eic_dev_cuda image. Log-only: the
test's PASS and hit dump land in the job log; no output dataset. Task
parameters follow the house shape (pcs/commands.py taskParamMap;
docs/JEDI_INTEGRATION.md).

Run on the production host with the operator token:

    source ~/.env
    export PANDA_AUTH=oidc PANDA_AUTH_VO=eic PANDA_CONFIG_ROOT=$HOME/.pathena
    export PANDA_URL_SSL=https://pandaserver01.sdcc.bnl.gov:25443/server/panda
    export PANDA_URL=http://pandaserver01.sdcc.bnl.gov:25080/server/panda
    python tools/npps0/submit_test_task.py [--version v01]
"""

import argparse
import sys

TASK_BASE = 'group.EIC.npps0.raindrop.test'
QUEUE = 'BNL_NPPS_GPU'
CONTAINER = '/home/wenaus/images/eic_dev_cuda_nightly.sif'
EXEC = ('bash -c "nvidia-smi -L && '
        'git clone --depth 1 https://github.com/BNLNPPS/simphony.git && '
        'cd simphony/dd4hepplugins/examples && '
        'python3 test_raindrop_dd4hep_gpu.py"')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', default='v01')
    args = ap.parse_args()

    task_name = f'{TASK_BASE}.{args.version}'
    params = {
        'taskName': task_name,
        'userName': 'wenaus',
        'vo': 'eic',
        'workingGroup': 'EIC',
        'campaign': '',
        'prodSourceLabel': 'managed',
        'taskType': 'production',
        'processingType': 'gputest',
        'taskPriority': 1000,
        'transPath': 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
        'transUses': '',
        'transHome': '',
        'architecture': '',
        'container_name': CONTAINER,
        'noInput': True,
        'nFiles': 1,
        'nFilesPerJob': 1,
        'coreCount': 8,
        'ramCount': 4000,
        'ramUnit': 'MBPerCore',
        'skipScout': True,
        'site': QUEUE,
        'cloud': 'EIC',
        'log': {
            'dataset': f'group.EIC:{task_name}.log',
            'type': 'template',
            'param_type': 'log',
            'token': 'local',
            'destination': 'local',
            'value': f'raindrop.$PANDAID.log.${{SN}}.log.tgz',
        },
        'jobParameters': [
            {'type': 'constant', 'value': EXEC},
        ],
    }

    from pandaclient import Client
    status, result = Client.insertTaskParams(params)
    print('client status:', status)
    print('server result:', result)
    if status != 0 or not (isinstance(result, (list, tuple)) and result[0] in (0, True)):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
