"""Image capability of the pilot pass npps0 runs for BNL_NPPS_GPU.

npps0 has no Harvester and no host reporter (NPPS0_WORKER.md): a launcher
loop runs the pass script, which runs the standard CVMFS wrapper and the ePIC
pilot against the queue data in git. With that queue data's ``container_type``
of ``apptainer:pilot`` the pilot's ePIC container layer starts each job in the
image the job declares, so the queue serves any image the job names. Observe
the committed pass script and queue data, as Perlmutter's launcher is observed
(perlmutter_pilots), rather than writing an image list by hand. This describes
the pass's binding, not evidence of a running or successful pilot.
"""
import json
import urllib.request

QUEUE = 'BNL_NPPS_GPU'
BASE = 'https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/tools/npps0/'
PASS = BASE + 'epicprod-gpu-pilot.sh'
QUEUEDATA = BASE + 'config/queuedata.json'
PRODUCT_KEY = 'npps0_pilot_runtime'
TTL_SECONDS = 300
LIMIT = 128 * 1024
PER_JOB_CONTAINERS = ('apptainer:pilot', 'singularity:pilot')


def _fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read(LIMIT + 1)


def read_capability(fetch=None):
    """Read only the committed pass script and queue data, each bounded to
    ten seconds and 128 KiB."""
    fetch = fetch or _fetch
    cap = {'queue': QUEUE, 'mode': 'unknown', 'images': [], 'source': PASS}
    try:
        script, queuedata = fetch(PASS), fetch(QUEUEDATA)
        if len(script) > LIMIT or len(queuedata) > LIMIT:
            raise ValueError('pass script or queue data exceeds the observation size limit')
        text = script.decode('utf-8')
        for marker in ('QUEUE=' + QUEUE + '\n',
                       'WRAPPER=/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh\n'):
            if marker not in text:
                raise ValueError('the pass no longer runs the standard wrapper for ' + QUEUE)
        data = json.loads(queuedata.decode('utf-8'))
        data = data.get(QUEUE, data) if isinstance(data, dict) else {}
        container = (data or {}).get('container_type')
        if container not in PER_JOB_CONTAINERS:
            raise ValueError(f'queue data container_type {container!r} does not start the job image')
        cap.update(mode='any', container_type=container, queuedata=QUEUEDATA)
    except (OSError, ValueError) as exc:
        cap.update(mode='unknown', images=[], error='npps0 pilot pass unreadable: ' + str(exc))
    return cap


def observed_capability(product, max_age_seconds):
    """A stale successful read cannot authorize dispatch."""
    age = product.get('age_seconds')
    value = product.get('value') or {}
    if value.get('queue') == QUEUE and age is not None and 0 <= age <= max_age_seconds:
        return dict(value, host='npps0-pass', age_seconds=age)
    return {'queue': QUEUE, 'mode': 'unknown', 'images': [],
            'host': 'npps0-pass', 'error': 'no fresh npps0 pilot pass report'}


def capability(max_age_seconds=900, refresh=False):
    from monitor_app.cached_product import get_product
    product = get_product(PRODUCT_KEY, read_capability, ttl_seconds=TTL_SECONDS,
                          refresh=refresh, async_first_fill=True)
    return observed_capability(product, max_age_seconds)
