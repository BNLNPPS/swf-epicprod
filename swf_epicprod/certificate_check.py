"""Host certificate check for the PanDA and OSG service hosts.

Reads the certificate chain each host serves and reports, per host, how
many days the leaf certificate has left and whether an intermediate was
served with it. A certificate served alone cannot be verified by a
browser that does not already hold the issuing intermediate, which is
the ordinary case for anyone outside the grid trust configuration.

Run as a module (``python -m swf_epicprod.certificate_check``), normally
as a nightly catalog-sync chain step on the production operations agent.
Prints a JSON summary and exits 0 when every host serves a complete
chain with more than the warning threshold remaining
(``CERTIFICATE_EXPIRY_WARN_DAYS``, default 7), 3 when any is inside the
threshold or serves no intermediate, 4 when any is expired or
unreachable.

The hosts are the production service faces: the PanDA server and its
monitor, this host's own web face, and the OSG submit host. Override
with ``CERTIFICATE_HOSTS``, a comma-separated list of ``host:port``.
"""

import json
import os
import re
import subprocess
import sys
import time

WARN_DAYS = float(os.environ.get('CERTIFICATE_EXPIRY_WARN_DAYS', '7'))
DEFAULT_HOSTS = (
    'pandaserver01.sdcc.bnl.gov:25443',
    'pandamon01.sdcc.bnl.gov:443',
    'pandaserver02.sdcc.bnl.gov:443',
    'osgsub01.sdcc.bnl.gov:443',
)
CONNECT_TIMEOUT = 20
PEM_RE = re.compile(
    r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', re.S)


def configured_hosts():
    raw = os.environ.get('CERTIFICATE_HOSTS', '').strip()
    if not raw:
        return list(DEFAULT_HOSTS)
    return [h.strip() for h in raw.split(',') if h.strip()]


def _served_chain(host, port):
    """The PEM blocks the host serves, leaf first. Raises RuntimeError
    when the handshake yields none."""
    out = subprocess.run(
        ['openssl', 's_client', '-connect', f'{host}:{port}',
         '-servername', host, '-showcerts'],
        input='', capture_output=True, text=True, timeout=CONNECT_TIMEOUT)
    blocks = PEM_RE.findall(out.stdout or '')
    if not blocks:
        reason = (out.stderr or out.stdout or '').strip().splitlines()
        raise RuntimeError(reason[-1] if reason else 'no certificate served')
    return blocks


def _leaf_dates(pem):
    """(not_after epoch, subject CN) of a PEM certificate."""
    out = subprocess.run(
        ['openssl', 'x509', '-noout', '-enddate', '-subject'],
        input=pem, capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip())
    not_after, subject = '', ''
    for line in (out.stdout or '').splitlines():
        if line.startswith('notAfter='):
            not_after = line.split('=', 1)[1].strip()
        elif line.startswith('subject='):
            subject = line.split('=', 1)[1].strip()
    import calendar
    parsed = time.strptime(not_after, '%b %d %H:%M:%S %Y %Z')
    return float(calendar.timegm(parsed)), subject


def check_certificates(hosts=None):
    results = []
    for host_port in (hosts if hosts is not None else configured_hosts()):
        host, _, port = host_port.partition(':')
        port = port or '443'
        entry = {'host': host, 'port': int(port),
                 'label': host.split('.')[0]}
        try:
            chain = _served_chain(host, port)
            expiry, subject = _leaf_dates(chain[0])
            days = (expiry - time.time()) / 86400.0
            entry['days_left'] = round(days, 2)
            entry['not_after'] = time.strftime(
                '%Y-%m-%d', time.gmtime(expiry))
            entry['subject'] = subject
            entry['chain_length'] = len(chain)
            entry['intermediate_served'] = len(chain) > 1
            if days <= 0:
                entry['status'] = 'expired'
            elif days <= WARN_DAYS:
                entry['status'] = 'expiring'
            else:
                entry['status'] = 'ok'
        except Exception as e:
            entry.update(status='unreachable', reason=str(e))
        results.append(entry)
    return results


def main():
    results = check_certificates()
    worst = 0
    for r in results:
        if r['status'] in ('expired', 'unreachable'):
            worst = max(worst, 4)
        elif r['status'] == 'expiring' or not r.get('intermediate_served',
                                                    True):
            worst = max(worst, 3)
    print(json.dumps({'warn_days': WARN_DAYS, 'certificates': results}))
    return worst


if __name__ == '__main__':
    sys.exit(main())
