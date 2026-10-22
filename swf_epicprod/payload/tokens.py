#!/usr/bin/env python3
"""Tokens for the payload, from the PanDA server, with the pilot's own token.

rucio:   python tokens.py rucio --out FILE --cfg FILE [--base-cfg FILE]
         writes the Rucio token to --out and a rucio.cfg that uses it to --cfg.
storage: python tokens.py storage --door root://host:port --path /storage/path
         [--base-path /strip] [--verb storage.create]
         prints a storage token for that one path.
patched-client: python tokens.py patched-client --dir DIR
         prints the lib directory of the payload's patched Rucio client,
         unpacked under DIR, when the image's client needs it; exit 4 when
         the image's own client uploads with tokens.
Exit 3 when the job has neither a PanDA token nor the pilot's proxy to sign
in to the PanDA server with (the caller keeps the JLab X.509 proxy).
"""

import argparse
import configparser
import os
import posixpath
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import requests

PANDA_URL = os.environ.get('EPICPROD_PANDA_URL', 'https://pandaserver01.sdcc.bnl.gov:25443')
RUCIO_CLIENT = os.environ.get('EPICPROD_RUCIO_TOKEN_CLIENT', 'rucio')
STORAGE_CLIENT = os.environ.get('EPICPROD_STORAGE_TOKEN_CLIENT', 'storage')
NO_PANDA_TOKEN = 3
IMAGE_CLIENT_UPLOADS_WITH_TOKENS = 4

# The July images carry the Rucio client 40.2.0 without the token upload
# patch (eic/eic-spack#1072), so their upload client hands the Rucio session
# token to the storage. The payload ships that client and the patch
# (rucio-clients.VERSION), unpacked and patched in the job where needed.
HERE = os.path.dirname(os.path.abspath(__file__))
RUCIO_CLIENT_SOURCE = os.path.join(HERE, 'rucio-clients-40.2.0.tgz')
RUCIO_CLIENT_PATCH = os.path.join(HERE, 'rucio-clients-token-upload.patch')
RUCIO_CLIENT_VERSION = '40.2.0'


class NoPandaToken(Exception):
    pass


def panda_auth():
    refreshed = os.environ.get('OIDC_REFRESHED_AUTH_TOKEN')
    name = refreshed if refreshed and os.path.exists(refreshed) else \
        os.environ.get('OIDC_AUTH_TOKEN', os.environ.get('PANDA_AUTH_TOKEN'))
    origin = os.environ.get('OIDC_AUTH_ORIGIN', os.environ.get('PANDA_AUTH_ORIGIN'))
    if not name or not origin:
        raise NoPandaToken('no PanDA token in the environment')
    if os.path.isabs(name) and os.path.exists(name):
        path = name
    else:
        base = os.environ.get('OIDC_AUTH_DIR', os.environ.get('PANDA_AUTH_DIR', os.environ.get('X509_USER_PROXY', '')))
        candidates = [os.path.dirname(base)] + [os.environ.get(v, '') for v in ('PILOT_SOURCE_DIR', 'PILOT_WORK_DIR', 'HOME')]
        path = next((os.path.join(d, name) for d in candidates if d and os.path.exists(os.path.join(d, name))), None)
        if not path:
            raise NoPandaToken(f'PanDA token file {name} not found')
    with open(path) as handle:
        return handle.read().strip(), origin


def panda_login():
    """How the job signs in to the PanDA server, as requests arguments: the
    pilot's PanDA token when it carries one, else the pilot's own proxy
    (harvester's robot credential), which run.sh keeps as
    PILOT_X509_USER_PROXY before the sandbox environment points
    X509_USER_PROXY at the JLab proxy. The server gives tokens to an
    identity with 'p' in its GRIDPREF."""
    try:
        token, origin = panda_auth()
        return {'headers': {'Authorization': f'Bearer {token}', 'Origin': origin}}
    except NoPandaToken:
        proxy = os.environ.get('PILOT_X509_USER_PROXY', '')
        if proxy and os.path.isfile(proxy):
            return {'cert': proxy}
        raise NoPandaToken('no PanDA token or pilot proxy in the environment')


def panda_token(client_name, audience=None, scope=None, timeout=60):
    login = panda_login()
    # Without Accept: application/json the server answers with the Python
    # repr of its reply, which is not JSON.
    login['headers'] = dict(login.get('headers') or {}, Accept='application/json')
    params = {'client_name': client_name}
    if audience:
        params['audience'] = audience
    if scope:
        params['scope'] = scope
    reply = requests.get(f'{PANDA_URL}/api/v1/creds/get_access_token', params=params,
                         verify=os.environ.get('X509_CERT_DIR', True), timeout=timeout,
                         **login)
    reply.raise_for_status()
    body = reply.json()
    if not body.get('success'):
        raise RuntimeError(f"PanDA refused a {client_name} token: {body.get('message')}")
    data = body.get('data')
    value = data.get('user_proxy') if isinstance(data, dict) else data
    if not value:
        raise RuntimeError(f'PanDA returned no {client_name} token')
    return value.strip()


def scope_path(path, base_path=''):
    path = posixpath.normpath('/' + path.lstrip('/'))
    if base_path:
        base = posixpath.normpath('/' + base_path.lstrip('/'))
        if base != '/' and (path == base or path.startswith(base + '/')):
            path = '/' + path[len(base):].lstrip('/')
    return path


def storage_token(door, path, base_path='', verbs=('storage.create',)):
    audience = urlparse(door).hostname
    sp = scope_path(path, base_path)
    return panda_token(STORAGE_CLIENT, audience=audience, scope=' '.join(f'{v}:{sp}' for v in verbs))


def xrd_env(token):
    env = dict(os.environ)
    env['XrdSecPROTOCOL'] = 'ztn'
    env['BEARER_TOKEN'] = token
    env.pop('BEARER_TOKEN_FILE', None)
    return env


def rucio_token_file(cfg_file=None):
    """The token file RUCIO_CONFIG names, when it asks for token auth; else None."""
    cfg_file = cfg_file or os.environ.get('RUCIO_CONFIG')
    if not cfg_file or not os.path.exists(cfg_file):
        return None
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(cfg_file)
    if cfg.get('client', 'auth_type', fallback='').strip() != 'oidc':
        return None
    return cfg.get('client', 'auth_token_file_path', fallback='').strip() or None


def rucio_client():
    """A Rucio Client made with this job's token when RUCIO_CONFIG asks for one.
    With oidc the client takes the first token WLCG discovery finds (BEARER_TOKEN,
    BEARER_TOKEN_FILE, $XDG_RUNTIME_DIR/bt_u<uid>, /tmp/bt_u<uid>) before its
    auth_token_file_path, and only when it is made; so BEARER_TOKEN_FILE names
    ours while it is made, and a stray token on the node never stands in."""
    from rucio.client import Client
    token_file = rucio_token_file()
    saved = {k: os.environ.pop(k, None) for k in ('BEARER_TOKEN', 'BEARER_TOKEN_FILE')}
    if token_file:
        os.environ['BEARER_TOKEN_FILE'] = token_file
    try:
        return Client()
    finally:
        os.environ.pop('BEARER_TOKEN_FILE', None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})


def write_rucio_config(token_file, cfg_file, base_cfg=None):
    cfg = configparser.ConfigParser(interpolation=None)
    if base_cfg and os.path.exists(base_cfg):
        cfg.read(base_cfg)
    if not cfg.has_section('client'):
        cfg.add_section('client')
    cfg.set('client', 'auth_type', 'oidc')
    cfg.set('client', 'auth_token_file_path', token_file)
    cfg.remove_option('client', 'client_x509_proxy')
    if os.environ.get('X509_CERT_DIR'):
        cfg.set('client', 'ca_cert', os.environ['X509_CERT_DIR'])
    with open(cfg_file, 'w') as handle:
        cfg.write(handle)


def image_client():
    """(version, uploads with tokens) of the image's Rucio client."""
    from rucio import version
    import rucio.client.uploadclient as uploadclient
    with open(uploadclient.__file__) as handle:
        return version.version_string(), 'get_upload_token' in handle.read()


def patched_client(dest):
    """The lib directory of the shipped Rucio client, unpacked and patched
    under dest, once per job."""
    lib = os.path.join(dest, 'lib')
    done = os.path.join(dest, '.patched')
    if os.path.exists(done):
        return lib
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    for cmd in (['tar', 'xzf', RUCIO_CLIENT_SOURCE, '-C', dest, '--strip-components=1'],
                ['patch', '-p1', '-s', '-i', RUCIO_CLIENT_PATCH]):
        step = subprocess.run(cmd, cwd=dest, capture_output=True, text=True)
        if step.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed: {(step.stderr or step.stdout).strip()[-300:]}")
    open(done, 'w').close()
    return lib


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='what', required=True)
    r = sub.add_parser('rucio')
    r.add_argument('--out', required=True)
    r.add_argument('--cfg', required=True)
    r.add_argument('--base-cfg', default=os.environ.get('RUCIO_CONFIG'))
    s = sub.add_parser('storage')
    s.add_argument('--door', required=True)
    s.add_argument('--path', required=True)
    s.add_argument('--base-path', default='')
    s.add_argument('--verb', action='append', default=None)
    p = sub.add_parser('patched-client')
    p.add_argument('--dir', required=True)
    args = parser.parse_args()
    try:
        if args.what == 'patched-client':
            version, uploads_with_tokens = image_client()
            if uploads_with_tokens:
                sys.exit(IMAGE_CLIENT_UPLOADS_WITH_TOKENS)
            if version != RUCIO_CLIENT_VERSION:
                raise RuntimeError(f'the image has Rucio client {version} without the token '
                                   f'upload patch, and the payload ships {RUCIO_CLIENT_VERSION}')
            print(patched_client(os.path.abspath(args.dir)))
        elif args.what == 'rucio':
            token = panda_token(RUCIO_CLIENT)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(token)
            write_rucio_config(os.path.abspath(args.out), args.cfg, args.base_cfg)
        else:
            print(storage_token(args.door, args.path, args.base_path, tuple(args.verb or ['storage.create'])))
    except NoPandaToken as exc:
        print(f'tokens: {exc}', file=sys.stderr)
        sys.exit(NO_PANDA_TOKEN)
    except Exception as exc:  # noqa: BLE001
        print(f'tokens: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
