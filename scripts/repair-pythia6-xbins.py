#!/usr/bin/env python3
"""Bounded HTTP steps for the approved pythia6-lhapdf edition repair.

Dry run by default. Apply the tags phase, run pcs_rebind_editions with its
reviewed edition count, then apply the proposals phase. The companion JSON
contains only the six registered samples covered by this repair.
"""
import argparse
import json
import os
from pathlib import Path

import requests
from bs4 import BeautifulSoup


TAGS = {
    'p4491': ('ep', '130', 'x_1e-3to1e-2'),
    'p4492': ('eAu', '100', 'x_1e-2to1e-1'),
    'p4493': ('eAg', '115', 'x_1e-4to1e-3'),
}
PLAN = Path(__file__).with_name('repair-pythia6-xbins-20261027.json')


def checked_json(response):
    try:
        value = response.json()
    except ValueError as error:
        raise RuntimeError(
            f'{response.request.method} {response.url}: HTTP '
            f'{response.status_code}, expected JSON') from error
    if not response.ok:
        raise RuntimeError(f'HTTP {response.status_code}: {value}')
    return value


def login(session, base):
    page = session.get(base + '/accounts/login/', timeout=30)
    page.raise_for_status()
    token = BeautifulSoup(page.text, 'html.parser').select_one(
        'input[name=csrfmiddlewaretoken]')
    if token is None:
        raise RuntimeError('Login page has no CSRF token')
    result = session.post(page.url, data={
        'username': os.environ['SWF_REMOTE_CLAUDE_USER'],
        'password': os.environ['SWF_REMOTE_CLAUDE_PASSWORD'],
        'csrfmiddlewaretoken': token['value'], 'next': '/prod/',
    }, headers={'Referer': page.url}, timeout=30)
    result.raise_for_status()
    if '/accounts/login/' in result.url:
        raise RuntimeError('PCS login did not complete')
    session.headers.update({
        'Referer': base + '/',
        'X-CSRFToken': session.cookies.get('csrftoken', ''),
    })


def tag_changes(session, base):
    changes = []
    for label, (species, hadron, window) in TAGS.items():
        url = base + '/pcs/api/physics-tags/' + label[1:] + '/'
        tag = checked_json(session.get(url, timeout=30))
        if tag['status'] != 'draft' or tag['lifecycle'] != 'active':
            raise RuntimeError(f'{label} is not an active draft')
        params = dict(tag.get('parameters') or {})
        expected = {'process': 'SIDIS', 'beam_species': species,
                    'beam_energy_electron': '9', 'beam_energy_hadron': hadron}
        if any(str(params.get(key, '')) != value for key, value in expected.items()):
            raise RuntimeError(f'{label} physics differs from the approved repair')
        if params.get('x_range') not in (None, '', window):
            raise RuntimeError(f'{label} already has a different x_range')
        updated = dict(params, x_range=window)
        changes.append((label, url, params, updated))
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('tags', 'proposals'), required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--base-url', default=os.environ.get(
        'SWFMON_URL', 'https://epic-devcloud.org/prod'))
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    with requests.Session() as session:
        login(session, base)
        changes = tag_changes(session, base)
        if args.phase == 'tags':
            for label, url, old, new in changes:
                print(json.dumps({'tag': label, 'before': old, 'after': new,
                                  'apply': args.apply and old != new}), flush=True)
            if args.apply:
                for label, url, old, new in changes:
                    if old == new:
                        continue
                    saved = checked_json(session.patch(
                        url, json={'parameters': new}, timeout=30))
                    if saved.get('parameters') != new:
                        raise RuntimeError(f'{label} readback differs from the patch')
            return
        if any(old != new for _label, _url, old, new in changes):
            raise RuntimeError('The three x_range corrections are not applied')
        plan = json.loads(PLAN.read_text())
        items = plan['items']
        if len(items) != 6 or len({item['did'] for item in items}) != 6:
            raise RuntimeError('The repair must contain exactly six distinct samples')
        if any('/pythia6-lhapdf/1.1.0/' not in item['did']
               or '/x_' not in item['did'] or item['campaign'] != '26.07'
               for item in items):
            raise RuntimeError('A sample is outside the approved repair')
        payload = {'action': 'registered_sample', 'items': items,
                   'proposer': 'registered-sample',
                   'batch_id': 'pythia6-xbin-repair-20261027'}
        print(json.dumps(payload), flush=True)
        if args.apply:
            result = checked_json(session.post(
                base + '/ai/api/proposals/propose/', json=payload, timeout=60))
            print(json.dumps(result), flush=True)
            if result.get('invalid') or result.get('denied'):
                raise RuntimeError('Some repair proposals were refused')


if __name__ == '__main__':
    main()
