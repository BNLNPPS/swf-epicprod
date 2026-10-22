"""The production operations agent's credentials for the JLab catalog and
the storage doors: tokens from the PanDA server, the eicprod proxy kept as
fallback.

The payload gets its tokens from the PanDA server (EPICPROD_PAYLOAD.md,
item 22); the agent's own work at JLab gets them the same way, signing in
with the robot credential it already holds for BNL Rucio
(``X509_USER_PROXY``, longproxy-for-rucio, the ``iddssv1`` identity with
``p`` in its GRIDPREF). The eicprod proxy (``EVGEN_X509_PROXY``) is a
certificate that cannot be renewed past 2026-10-28; until then it is the
fallback when no token comes.

- ``rucio_client``: a JLab Rucio client as the production account, by
  token, else by the proxy when one is given and present.
- ``door_access``: the URL and environment for an xrootd command at a
  storage door: a path-scoped storage token over TLS for a door that
  takes PanDA's storage tokens (``EPICPROD_TOKEN_DOORS``, host:port,
  BNL-XRD's write door epicxrd1:1094 by default), else the proxy when one
  is given, else no credential. BNL-XRD's read door (1095) speaks no TLS
  and reads without a credential. JLab's doors take no PanDA token: the
  server's storage client mints none for dtn-rucio (2026-10-22).

Every client made records how it authenticated (``client.jlab_auth``,
``token`` or ``x509``), so a pass can report which credential it ran on.
"""

import base64
import json
import os
import posixpath
import threading
import time
from urllib.parse import urlparse

PANDA_URL = os.environ.get('EPICPROD_PANDA_URL', 'https://pandaserver01.sdcc.bnl.gov:25443')
RUCIO_TOKEN_CLIENT = os.environ.get('EPICPROD_RUCIO_TOKEN_CLIENT', 'rucio')
STORAGE_TOKEN_CLIENT = os.environ.get('EPICPROD_STORAGE_TOKEN_CLIENT', 'storage')
JLAB_RUCIO_URL = os.environ.get('JLAB_RUCIO_URL', 'https://rucio-server.jlab.org:443')
JLAB_RUCIO_ACCOUNT = os.environ.get('EVGEN_RUCIO_ACCOUNT', 'eicprod')
TOKEN_DOORS = {h.strip() for h in os.environ.get(
    'EPICPROD_TOKEN_DOORS', 'epicxrd1.sdcc.bnl.gov:1094').split(',') if h.strip()}
# A cached token is used while it has this long left.
TOKEN_MARGIN_S = 600

_lock = threading.Lock()
_cache = {}


class NoToken(Exception):
    """The PanDA server gave no token."""


def _login_proxy():
    return os.environ.get('EPICPROD_PANDA_LOGIN_PROXY') or os.environ.get('X509_USER_PROXY', '')


def _ca():
    path = os.environ.get('X509_CERT_DIR') or '/etc/grid-security/certificates'
    return path if os.path.isdir(path) else True


def _expiry(token):
    """The token's exp claim, or 0 when it cannot be read."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get('exp') or 0)
    except Exception:                                         # noqa: BLE001
        return 0.0


def panda_token(client_name, audience=None, scope=None, timeout=60):
    """A token of the PanDA server's ``client_name`` token client, asked for
    with the robot proxy; reused from this process's cache while it has
    TOKEN_MARGIN_S left. Raises NoToken with the reason."""
    import requests
    key = (client_name, audience or '', scope or '')
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] - time.time() > TOKEN_MARGIN_S:
            return hit[0]
    proxy = _login_proxy()
    if not proxy or not os.path.isfile(proxy):
        raise NoToken(f'no proxy to sign in to the PanDA server with ({proxy!r})')
    params = {'client_name': client_name}
    if audience:
        params['audience'] = audience
    if scope:
        params['scope'] = scope
    try:
        # Without Accept: application/json the server answers with the
        # Python repr of its reply, which is not JSON.
        reply = requests.get(f'{PANDA_URL}/api/v1/creds/get_access_token', params=params,
                             cert=proxy, verify=_ca(), timeout=timeout,
                             headers={'Accept': 'application/json'})
        reply.raise_for_status()
        body = reply.json()
    except Exception as e:                                    # noqa: BLE001
        raise NoToken(f'PanDA token request failed: {e}')
    if not body.get('success'):
        raise NoToken(f"PanDA refused a {client_name} token: {body.get('message')}")
    data = body.get('data')
    token = (data.get('user_proxy') if isinstance(data, dict) else data) or ''
    token = token.strip()
    if not token:
        raise NoToken(f'PanDA returned no {client_name} token')
    with _lock:
        _cache[key] = (token, _expiry(token) or time.time() + 3600)
    return token


def _token_client(host, account, timeout):
    """A Rucio client authenticated by the rucio token. The client takes the
    first token WLCG discovery finds when it is made, so BEARER_TOKEN names
    ours for that moment only, and a stray token never stands in. A
    ``timeout`` bounds the token request as well as the client."""
    from rucio.client import Client
    token = panda_token(RUCIO_TOKEN_CLIENT, timeout=timeout or 60)
    saved = {k: os.environ.get(k) for k in ('BEARER_TOKEN', 'BEARER_TOKEN_FILE')}
    with _lock:
        try:
            os.environ['BEARER_TOKEN'] = token
            os.environ.pop('BEARER_TOKEN_FILE', None)
            kwargs = {'timeout': timeout} if timeout else {}
            client = Client(rucio_host=host, auth_host=host, account=account,
                            auth_type='oidc', creds={'oidc_auto': False}, **kwargs)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    client.jlab_auth = 'token'
    return client


def rucio_client(account=None, host=None, proxy=None, timeout=None):
    """A JLab Rucio client as ``account`` (the production account by
    default): by token, verified with whoami, else by ``proxy`` when one is
    given and present. Raises RuntimeError naming both reasons when neither
    works."""
    from rucio.client import Client
    account = account or JLAB_RUCIO_ACCOUNT
    host = host or JLAB_RUCIO_URL
    try:
        client = _token_client(host, account, timeout)
        who = client.whoami()
        if who.get('account') != account:
            raise NoToken(f"the token maps to account {who.get('account')!r}, not {account!r}")
        return client
    except Exception as e:                                    # noqa: BLE001
        why = f'token: {e}'
    if not proxy or not os.path.exists(proxy):
        raise RuntimeError(f'{why}; no proxy to fall back to')
    kwargs = {'timeout': timeout} if timeout else {}
    try:
        client = Client(rucio_host=host, auth_host=host, account=account,
                        auth_type='x509_proxy', creds={'client_proxy': proxy}, **kwargs)
    except Exception as e:                                    # noqa: BLE001
        raise RuntimeError(f'{why}; proxy: {e}')
    client.jlab_auth = 'x509'
    return client


def door_access(door_url, path, verbs=('storage.read',), proxy=None):
    """(door URL, environment, how) for an xrootd command on ``path`` at a
    door. A door in TOKEN_DOORS gets a storage token for that path over TLS
    (roots://; the token protocol is refused without it) and no proxy in
    the environment; any other door, or a token door when no token comes,
    gets ``proxy``, or no credential when there is none. ``how`` is
    'token', 'x509' or 'none'."""
    parsed = urlparse(door_url)
    host = parsed.hostname or ''
    env = dict(os.environ)
    env.pop('BEARER_TOKEN_FILE', None)
    if f'{host}:{parsed.port}' in TOKEN_DOORS:
        # Catalogued PFNs carry doubled slashes (//eic/EPIC//TEST/...); the
        # token's scope is the plain path.
        scope_path = posixpath.normpath('/' + path.lstrip('/'))
        try:
            token = panda_token(STORAGE_TOKEN_CLIENT, audience=host,
                                scope=' '.join(f'{v}:{scope_path}' for v in verbs))
            env.pop('X509_USER_PROXY', None)
            env.update(XrdSecPROTOCOL='ztn', BEARER_TOKEN=token)
            env.setdefault('X509_CERT_DIR', '/etc/grid-security/certificates')
            netloc = parsed.netloc
            return f'roots://{netloc}', env, 'token'
        except NoToken:
            pass
    env.pop('BEARER_TOKEN', None)
    if proxy:
        env['X509_USER_PROXY'] = proxy
        return door_url, env, 'x509'
    env.pop('X509_USER_PROXY', None)
    return door_url, env, 'none'
