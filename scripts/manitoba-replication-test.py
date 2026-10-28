#!/usr/bin/env python3
"""The approved 28 October Manitoba-to-JLab trial replication, as eicprod.

Default: read the exact two DIDs, rules, locks and transfer diagnostics.
--apply --level RECO or FULL creates at most one 24-hour rule. FULL requires
RECO to be OK. No rule, quota, RSE, source replica or task is ever modified.
Run as the production operator with the monitor Python and ~/.env loaded.
Keep --state outside Git; its pending marker prevents blind POST retries.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time

from dotenv import dotenv_values
from rucio.common.exception import AccessDenied, DuplicateRule, RequestNotFound

from swf_epicprod.jlab_auth import rucio_client

ROOT = '/TEST/trial/group.EIC.26.07.3.epic_craterlake.p1108.e37.s11.r11.130to177deg.trial/'
ACCOUNT, SOURCE, TARGET = 'eicprod', 'MANITOBA-XRD', 'EIC-XRD'
COMMENT = 'Manitoba->JLab replication test'
LEVELS = ('RECO', 'FULL')
RULE_FIELDS = ('id', 'account', 'state', 'rse_expression', 'copies',
               'source_replica_expression', 'expires_at', 'error',
               'locks_ok_cnt', 'locks_replicating_cnt', 'locks_stuck_cnt')
REQUEST_FIELDS = ('request_id', 'state', 'source_rse', 'dest_rse',
                  'external_host', 'external_id', 'err_msg', 'updated_at',
                  'started_at', 'transferred_at')


def matching_rules(client, level):
    return [r for r in client.list_did_rules('epic', ROOT + level)
            if str(r['account']) == ACCOUNT
            and r['rse_expression'] == TARGET and r['copies'] == 1]


def diagnostics(client, files, rule):
    result = {'rule': {k: rule.get(k) for k in RULE_FIELDS}}
    result['locks'] = [{k: row.get(k) for k in ('name', 'state', 'rse')}
                       for row in client.list_replica_locks(rule['id'])]
    result['transfers'] = []
    result['diagnostic_errors'] = []
    if rule['state'] != 'OK':
        for entry in files:
            for historical, method in ((False, client.list_request_by_did),
                                       (True, client.list_request_history_by_did)):
                try:
                    reply = method(entry['name'], TARGET, scope='epic')
                    rows = [reply] if isinstance(reply, dict) else list(reply)
                except RequestNotFound:
                    rows = []
                except AccessDenied as error:
                    result['diagnostic_errors'].append({
                        'method': method.__name__, 'name': entry['name'],
                        'error': str(error)})
                    continue
                for row in rows:
                    result['transfers'].append({'name': entry['name'],
                        'historical': historical,
                        **{k: row.get(k) for k in REQUEST_FIELDS}})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--level', choices=LEVELS)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    if args.apply and not args.level:
        parser.error('--apply requires --level')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with open(str(args.state) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(args.state.read_text()) if args.state.exists() else {}

        def save():
            tmp = args.state.with_suffix('.tmp')
            with open(tmp, 'w') as output:
                os.chmod(tmp, 0o600)
                json.dump(state, output, indent=2, default=str)
                output.write('\n')
            tmp.replace(args.state)

        actor = os.environ['SWF_REMOTE_CLAUDE_USER']
        os.environ.update({k: v for k, v in dotenv_values(
            '/opt/swf-monitor/config/env/production.env').items() if v is not None})
        client = rucio_client(account=ACCOUNT, proxy=os.environ.get('EVGEN_X509_PROXY'), timeout=30)
        if client.whoami().get('account') != ACCOUNT:
            raise RuntimeError('Unexpected Rucio account')
        files = {level: list(client.list_files('epic', ROOT + level)) for level in LEVELS}
        count = sum(len(rows) for rows in files.values())
        size = sum(row['bytes'] for rows in files.values() for row in rows)
        if not all(files.values()) or count > 3 or size > 4_000_000:
            raise RuntimeError('DIDs are outside the approved small trial bounds')
        baseline = {level: sorted([{k: row[k] for k in ('name', 'bytes', 'adler32')}
                                  for row in rows], key=lambda row: row['name'])
                    for level, rows in files.items()}
        if state.get('files') not in (None, baseline):
            raise RuntimeError('Trial contents changed since the initial review')
        state.update(files=baseline, actor=actor, account=ACCOUNT, source=SOURCE,
                     target=TARGET, lifetime_seconds=86400,
                     authorization='Torre approved in swf-teamcomms on 2026-10-28',
                     observed_at=datetime.now(timezone.utc).isoformat())
        for replica in client.list_replicas([{'scope': 'epic', 'name': row['name']}
                                            for rows in files.values() for row in rows]):
            if replica.get('states', {}).get(SOURCE) != 'AVAILABLE':
                raise RuntimeError('Trial source is not AVAILABLE at MANITOBA-XRD')
        rules = {level: matching_rules(client, level) for level in LEVELS}
        if args.apply:
            level = args.level
            if level == 'FULL' and not any(r['state'] == 'OK' for r in rules['RECO']):
                raise RuntimeError('RECO must be OK before creating FULL')
            operation = state.setdefault('operations', {}).setdefault(level, {})
            if not rules[level]:
                if operation.get('pending'):
                    raise RuntimeError('Prior POST outcome uncertain; no rule is visible, so do not resend')
                operation.update(pending=True, requested_at=state['observed_at'])
                save()
                started = time.monotonic()
                try:
                    ids = client.add_replication_rule(
                        [{'scope': 'epic', 'name': ROOT + level}], 1, TARGET,
                        lifetime=86400, source_replica_expression=SOURCE, comment=COMMENT)
                except DuplicateRule:
                    ids = [r['id'] for r in matching_rules(client, level)]
                    if not ids:
                        raise
                except Exception as error:
                    operation.update(error=str(error), duration_ms=round((time.monotonic()-started)*1000))
                    save()
                    raise
                operation.update(pending=False, rule_ids=ids,
                                 duration_ms=round((time.monotonic()-started)*1000))
                save()
                rules[level] = matching_rules(client, level)
                if not rules[level]:
                    raise RuntimeError('POST returned but no matching rule is visible; inspect recorded IDs')
            else:
                operation.update(pending=False, rule_ids=[r['id'] for r in rules[level]],
                                 reused=True)
        state['results'] = {level: [diagnostics(client, files[level], rule)
                                   for rule in rules[level]] for level in LEVELS}
        save()
        print(json.dumps({'file_count': count, 'bytes': size, **state}, default=str))


if __name__ == '__main__':
    main()
