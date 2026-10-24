"""Image capability of the public launcher Perlmutter fetches at worker start.

The external Harvester has no host reporter here. Observe its actual fixed
launcher URL, rather than inventing a registry image list. This describes
the launcher's binding, not evidence of a running or successful worker.
"""
import hashlib
import re
import urllib.request

QUEUE = 'NERSC_Perlmutter_epic_es'
SOURCE = ('https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/'
          'perlmutter/' + QUEUE + '/epicprod-perlmutter-pilot-launch.sh')
PRODUCT_KEY = 'perlmutter_pilot_runtime'
TTL_SECONDS = 300


def read_capability(fetch=None):
    """Read only the public adapter, bounded to ten seconds and 128 KiB."""
    cap = {'queue': QUEUE, 'mode': 'unknown', 'images': [], 'source': SOURCE}
    try:
        if fetch is None:
            with urllib.request.urlopen(SOURCE, timeout=10) as response:
                data = response.read(128 * 1024 + 1)
        else:
            data = fetch(SOURCE)
        if len(data) > 128 * 1024:
            raise ValueError('launcher exceeds the observation size limit')
        text = data.decode('utf-8')
        cap['sha256'] = hashlib.sha256(data).hexdigest()
        for marker in ('export EPICPROD_NATIVE_BATCH=1',
                       'export EPICPROD_NATIVE_IMAGE=$IMAGE',
                       'bash "$EPICPROD_NATIVE_ASSETS/epic-native-pilot.sh"',
                       'verify "$RUNTIME_SHA256" native-runtime.tar.gz'):
            if marker not in text:
                raise ValueError('common native launcher binding is not established')
        def scalar(name, pattern):
            matches = re.findall(r'^' + name + '=(' + pattern + r')$', text, re.M)
            if len(matches) != 1:
                raise ValueError(name + ' is missing, conflicting or unresolved')
            return matches[0]
        image = scalar('IMAGE', r'/cvmfs/[A-Za-z0-9_./:+-]+')
        runtime = scalar('RUNTIME_URL',
                         r'https://epic-devcloud-stageout\.s3\.us-east-1\.amazonaws\.com/pilot/[A-Za-z0-9_.-]+')
        cap.update(mode='bound', images=[image], runtime_url=runtime,
                   runtime_sha256=scalar('RUNTIME_SHA256', r'[0-9a-f]{64}'),
                   pilot_archive_sha256=scalar('PILOT_SHA256', r'[0-9a-f]{64}'),
                   es_channel_sha256=scalar('ES_CHANNEL_SHA256', r'[0-9a-f]{64}'))
    except (OSError, ValueError) as exc:
        cap.update(mode='unknown', images=[], error='Perlmutter launcher unreadable: ' + str(exc))
    return cap


def observed_capability(product, max_age_seconds):
    """A stale successful read cannot authorize dispatch."""
    age = product.get('age_seconds')
    value = product.get('value') or {}
    if value.get('queue') == QUEUE and age is not None and 0 <= age <= max_age_seconds:
        return dict(value, host='perlmutter-launcher', age_seconds=age)
    return {'queue': QUEUE, 'mode': 'unknown', 'images': [],
            'host': 'perlmutter-launcher', 'error': 'no fresh Perlmutter native launcher report'}


def capability(max_age_seconds=900, refresh=False):
    from monitor_app.cached_product import get_product
    product = get_product(PRODUCT_KEY, read_capability, ttl_seconds=TTL_SECONDS,
                          refresh=refresh, async_first_fill=True)
    return observed_capability(product, max_age_seconds)
