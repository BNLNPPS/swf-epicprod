"""Submit a coprocessor driver task to BNL_NPPS_GPU.

One noInput/noOutput job whose payload clones swf-epicprod and runs the
self-contained coprocessor driver (driver.py): dispatcher, agent, and
interim executable spawned inside the job, a batch of gun units plus the
reference-set check unit, verdict in the job's exit code and the
COPROC-DRIVER line in its log. The task parameter map is the working
gputest shape of tools/npps0/submit_test_task.py.

Host paths (simphony install, geometry, reference set) are npps0-local:
this queue serves that host. --exec-mode direct because the pilot already
runs the payload inside the container.

Run on npps0 with the operator token:

    source ~/.env
    export PANDA_AUTH=oidc PANDA_AUTH_VO=EIC.production PANDA_CONFIG_ROOT=$HOME/.pathena
    export PANDA_URL_SSL=https://pandaserver01.sdcc.bnl.gov:25443/server/panda
    export PANDA_URL=http://pandaserver01.sdcc.bnl.gov:25080/server/panda
    PYTHONPATH=$HOME/github/panda-client \
        python3 submit_coprocessor_task.py [--version v01] [--units 3] [--count 100000]
"""

import argparse
import sys
import urllib.parse

TASK_BASE = 'group.EIC.npps0.coproc.test'
QUEUE = 'BNL_NPPS_GPU'
CONTAINER = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly'
SOURCE_URL = 'https://pandaserver01.sdcc.bnl.gov:25443'

PREFIX = '/home/wenaus/work/simphony-synrad-install'
REFSET = '/home/wenaus/work/synrad-refset-20260820-release'


def payload(args, task_name):
    # --outdir on the host: this queue serves one host, and with --no-log
    # there is no staged log, so the summary and unit records land in a
    # host directory as the run's evidence; the books carry the verdict.
    return ('nvidia-smi -L && '
            'git clone --depth 1 https://github.com/BNLNPPS/swf-epicprod.git && '
            'python3 swf-epicprod/tools/worker/coprocessor/driver.py'
            f' --units {args.units} --count {args.count}'
            f' --task-name {task_name} --executable oneshot --exec-mode direct'
            f' --prefix {PREFIX}'
            f' --geom synrad --geom-cfbase {REFSET}/geometry'
            f' --geom-edition synrad_bench-v1'
            f' --refset-input {REFSET}/inphoton/synrad_service_inphoton.npy'
            f' --outdir /home/wenaus/work/coproc/panda-runs/{task_name}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', default='v01')
    ap.add_argument('--units', type=int, default=3)
    ap.add_argument('--count', type=int, default=100000)
    # Tadashi 2026-08-14: tasks whose jobs produce no log files omit the
    # 'log' entry entirely; with no log dataset there is nothing for the
    # refiner to validate ('unknown endpoint: local', task 39012) and
    # nothing for the Adder to register.
    ap.add_argument('--no-log', action='store_true')
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
            'value': f'{task_name}.$PANDAID._${{SN}}.log.tgz',
            'dataset': f'{task_name}_log/',
            'destination': 'local',
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
             'value': urllib.parse.quote(payload(args, task_name), safe='')},
            {'type': 'constant', 'value': '"'},
        ],
    }

    if args.no_log:
        del params['log']

    from pandaclient import Client
    status, result = Client.insertTaskParams(params)
    print('client status:', status)
    print('server result:', result)
    return 0 if status == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
