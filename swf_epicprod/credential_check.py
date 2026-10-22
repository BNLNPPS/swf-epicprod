"""Production credential expiry check.

Checks the expiry of the credentials the production system depends on
and reports how many days each has left:

- the PanDA OIDC token (``$PANDA_CONFIG_ROOT/.token``, default
  ``~/.pathena/.token``) — automated submission stops when it expires;
- the BNL Rucio x509 proxy (``$X509_USER_PROXY``) — payload-log
  retrieval and Rucio metadata access;
- the EVGEN output x509 proxy (``$EVGEN_X509_PROXY``) — shipped in the
  submission sandbox for JLab Rucio output registration;
- the JLab token (``jlab_token``) — whether the PanDA server gives the
  agent a rucio token that JLab Rucio takes as eicprod
  (``swf_epicprod.jlab_auth``), the credential the payload and the agent
  write with since 2026-10-22. It has no expiry to report: each token is
  minted for hours, so it is ok or unavailable.

The eicprod proxy is the fallback where no token comes, and it cannot be
renewed past 2026-10-28. While the token is ok the proxy is reported as
superseded, with its days left, and does not count toward the exit code;
when the token is unavailable the proxy counts as before, and the token's
own state does not, since the proxy carries the work. So the check stops
production only when neither credential works.

Run as a module (``python -m swf_epicprod.credential_check``), normally
as a nightly catalog-sync chain step on the production operations
agent. Prints a JSON summary and exits 0 when every configured
credential has more than the warning threshold remaining
(``CREDENTIAL_EXPIRY_WARN_DAYS``, default 7), 3 when any is inside the
threshold, 4 when any is expired, missing, or unreadable. A credential
whose environment variable is unset is reported and counts toward
exit 3: the check cannot vouch for it.
"""

import base64
import json
import os
import subprocess
import sys
import time

WARN_DAYS = float(os.environ.get('CREDENTIAL_EXPIRY_WARN_DAYS', '7'))
# Each of the token request and the Rucio client's calls, seconds.
JLAB_TOKEN_TIMEOUT_S = 10


def _days_left(expiry_epoch):
    return (expiry_epoch - time.time()) / 86400.0


def _jwt_exp(path):
    """Expiry epoch of the PanDA token file: a JSON wrapper holding an
    id_token JWT, or a bare JWT."""
    raw = open(path).read().strip()
    token = raw
    try:
        token = json.loads(raw).get('id_token', raw)
    except ValueError:
        pass
    payload = token.split('.')[1]
    payload += '=' * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return float(claims['exp'])


def _x509_exp(path):
    """Expiry epoch of an x509 proxy/certificate via openssl."""
    out = subprocess.run(
        ['openssl', 'x509', '-enddate', '-noout', '-in', path],
        capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip())
    not_after = out.stdout.strip().split('=', 1)[1]
    parsed = time.strptime(not_after, '%b %d %H:%M:%S %Y %Z')
    import calendar
    return float(calendar.timegm(parsed))


def check_credentials():
    panda_root = os.environ.get(
        'PANDA_CONFIG_ROOT', os.path.expanduser('~/.pathena'))
    creds = [
        ('panda_oidc_token', 'jwt', os.path.join(panda_root, '.token')),
        ('bnl_rucio_proxy', 'x509', os.environ.get('X509_USER_PROXY', '')),
        ('evgen_output_proxy', 'x509', os.environ.get('EVGEN_X509_PROXY', '')),
    ]
    token = _jlab_token()
    results = [token]
    for name, kind, path in creds:
        entry = {'credential': name, 'path': path}
        if not path:
            entry.update(status='unset',
                         reason='environment variable not set')
        elif not os.path.exists(path):
            entry.update(status='missing', reason='file not found')
        else:
            try:
                exp = _jwt_exp(path) if kind == 'jwt' else _x509_exp(path)
                days = _days_left(exp)
                entry['days_left'] = round(days, 2)
                if days <= 0:
                    entry['status'] = 'expired'
                elif days <= WARN_DAYS:
                    entry['status'] = 'expiring'
                else:
                    entry['status'] = 'ok'
            except Exception as e:
                entry.update(status='unreadable', reason=str(e))
        if name == 'evgen_output_proxy' and token['status'] == 'ok' \
                and entry['status'] != 'ok':
            entry['superseded_by'] = entry['status']
            entry['status'] = 'superseded'
        results.append(entry)
    return results


def gate_days(results):
    """The days map the front's credential gate reads (front.credential_verdict):
    the credentials that expire and count. A token has no expiry, and a
    superseded proxy no longer counts, so neither is in it."""
    return {r['credential']: r.get('days_left') for r in results
            if r.get('kind') != 'token' and r.get('status') != 'superseded'}


def superseded_days(results):
    """The superseded credentials' days left, kept on the record apart."""
    return {r['credential']: r.get('days_left') for r in results
            if r.get('status') == 'superseded'}


def _jlab_token():
    """The JLab token's entry: ok when JLab Rucio takes the PanDA server's
    rucio token as the production account, unavailable with the reason
    otherwise. No days_left: a token lives hours, and its expiry is no
    renewal anyone must make."""
    entry = {'credential': 'jlab_token', 'kind': 'token', 'path': ''}
    try:
        from .jlab_auth import rucio_client
        # Bounded well inside the agent's 60 s for the whole check, so a
        # token service that hangs leaves the proxies still checked.
        client = rucio_client(proxy=None, timeout=JLAB_TOKEN_TIMEOUT_S)
        entry.update(status='ok', account=client.whoami().get('account'))
    except Exception as e:                                    # noqa: BLE001
        entry.update(status='unavailable', reason=str(e)[:300])
    return entry


def main():
    results = check_credentials()
    worst = 0
    for r in results:
        if r['status'] in ('superseded', 'unavailable', 'ok'):
            continue
        if r['status'] in ('expired', 'missing', 'unreadable'):
            worst = max(worst, 4)
        elif r['status'] in ('expiring', 'unset'):
            worst = max(worst, 3)
    print(json.dumps({'warn_days': WARN_DAYS, 'credentials': results}))
    return worst


if __name__ == '__main__':
    sys.exit(main())
