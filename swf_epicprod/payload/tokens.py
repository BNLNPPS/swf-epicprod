#!/usr/bin/env python3
"""Tokens for the payload, from the PanDA server, with the pilot's own token.

rucio:   python tokens.py rucio --out FILE --cfg FILE [--base-cfg FILE]
         writes the Rucio token to --out and a rucio.cfg that uses it to --cfg.
storage: python tokens.py storage --door root://host:port --path /storage/path
         [--base-path /strip] [--verb storage.create]
         prints a storage token for that one path.
Exit 3 when there is no PanDA token in the environment (the caller keeps X.509).
"""

import argparse
import configparser
import os
import posixpath
import sys
from urllib.parse import urlparse

import requests

PANDA_URL = os.environ.get('EPICPROD_PANDA_URL', 'https://pandaserver01.sdcc.bnl.gov:25443')
RUCIO_CLIENT = os.environ.get('EPICPROD_RUCIO_TOKEN_CLIENT', 'rucio')
STORAGE_CLIENT = os.environ.get('EPICPROD_STORAGE_TOKEN_CLIENT', 'storage')
NO_PANDA_TOKEN = 3


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


def panda_token(client_name, audience=None, scope=None, timeout=60):
    token, origin = panda_auth()
    params = {'client_name': client_name}
    if audience:
        params['audience'] = audience
    if scope:
        params['scope'] = scope
    reply = requests.get(f'{PANDA_URL}/api/v1/creds/get_access_token', params=params,
                         headers={'Authorization': f'Bearer {token}', 'Origin': origin},
                         verify=os.environ.get('X509_CERT_DIR', True), timeout=timeout)
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
    args = parser.parse_args()
    try:
        if args.what == 'rucio':
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
