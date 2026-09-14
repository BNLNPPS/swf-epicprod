"""Registered-sample proposer (docs/EPICPROD_EVGEN_INPUTS.md § From a
registered sample to a task; swf-monitor docs/AI_PROPOSALS.md, category
``registered_sample``).

The rule: a registered EVGEN dataset that no PCS evgen dataset matches,
that no request names and that no task runs is a sample nobody asked
for. For each one whose physics derives from its path, propose its
intake into the current production campaign; the approval supplies the
requestor, the event target and the priority. Comments are code-filled.
Rule-based: no model.

A dataset whose path does not derive proposes nothing and stays in the
unmatched list. Runs as the last step of the EVGEN assimilation, nightly
and on the Update button, and by hand; each run withdraws the pending
proposals of samples the record has since taken in and re-derives the
rest (the scan heartbeat). Switched off with the SysConfig key
``ai_propose_registered_sample``.
"""
import logging
import time

logger = logging.getLogger(__name__)

PROPOSER = 'registered-sample'
SWITCH_KEY = 'ai_propose_registered_sample'


def enabled():
    from monitor_app.models import SysConfig
    return bool(SysConfig.get_setting(SWITCH_KEY, True))


def _registered_records():
    """The recorded registered inventory: {path: record} with the
    dataset's files, events and registration date."""
    import json
    import os
    from pcs.services import EVGEN_RUCIO_SNAPSHOT_NAME, RUCIO_SNAPSHOT_DIR, _rucio_evgen_entry
    try:
        with open(os.path.join(RUCIO_SNAPSHOT_DIR, EVGEN_RUCIO_SNAPSHOT_NAME)) as f:
            records = json.load(f).get('datasets') or []
    except FileNotFoundError:
        return {}
    out = {}
    for record in records:
        entry = _rucio_evgen_entry(record)
        did = str(entry.get('did') or '')
        path = '/' + did.partition(':')[2].lstrip('/')
        replicas = record.get('rse_replicas') or []
        files = max([int(r.get('length') or 0) for r in replicas] + [int(record.get('length') or 0)])
        created = min([str(r.get('created_at') or '') for r in replicas if r.get('created_at')] or [''])
        out[path] = {'did': f'epic:{path}', 'files': files,
                     'events': record.get('events'), 'registered_at': created}
    return out


def _comment(path, record, identity):
    physics = identity['physics'] or {}
    evgen = identity['evgen'] or {}
    parts = [f"Registered {record['registered_at'] or 'at an unrecorded date'}: "
             f"{record['files'] or '?'} file(s), "
             f"{record['events'] if record['events'] is not None else 'an uncounted number of'} events; "
             f"no request names it and no task runs it."]
    reading = ' '.join(str(physics.get(k) or '') for k in (
        'process', 'beam_energy_electron', 'beam_energy_hadron', 'beam_species', 'q2_range')
        if physics.get(k))
    gen = ' '.join(p for p in (evgen.get('generator'), evgen.get('generator_version')) if p)
    if evgen.get('radiative'):
        gen += f" radiative {evgen['radiative']}"
    parts.append(f"Physics read from the path: {reading or 'background'}; generator {gen or 'unresolved'}"
                 + (f"; sample {identity['sample']}" if identity.get('sample') else '')
                 + (f"; physics tag {identity['physics_tag']}" if identity.get('physics_tag') else '; a new physics tag')
                 + (f"; configuration {identity['pc']}" if identity.get('pc') else '') + '.')
    return ' '.join(parts)


def findings():
    """The samples to propose: [(path, record, identity)] for every
    registered dataset the record does not hold whose path derives."""
    from pcs.models import Dataset
    from pcs.registered_samples import (derive_registered_sample,
                                        registered_sample_precondition)
    matched = set()
    for ds in Dataset.objects.filter(metadata__contains={'stage': 'evgen'}):
        for m in ((ds.metadata or {}).get('rucio') or {}).get('matched') or []:
            matched.add('/' + str(m.get('did') or '').partition(':')[2].lstrip('/'))
    out = []
    for path, record in sorted(_registered_records().items()):
        if path in matched:
            continue
        held = registered_sample_precondition(path)
        if held['matched_by'] or held['requested_by'] or held['tasks']:
            continue
        identity, _reason = derive_registered_sample(path)
        if identity is None:
            continue
        out.append((path, record, identity))
    return out


def propose_registered_samples(*, created_by='', batch_id='', apply=True):
    """Withdraw pending proposals of samples the record has taken in,
    then propose the current findings. Returns the counts."""
    from ai.models import Proposal
    from ai.services import propose_registered_samples as _propose
    from pcs.models import Campaign
    result = {'enabled': enabled(), 'findings': 0, 'proposed': 0, 'noop': 0,
              'denied': 0, 'invalid': 0, 'withdrawn': 0, 'errors': []}
    if not result['enabled']:
        return result
    campaign = Campaign.objects.filter(lifecycle='current').first()
    if campaign is None:
        result['errors'].append('no current campaign')
        return result
    batch_id = batch_id or f'{PROPOSER}-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}'
    found = findings()
    result['findings'] = len(found)
    current = {f'epic:{path}' for path, _r, _i in found}
    if not apply:
        return result
    for row in Proposal.objects.filter(proposer=PROPOSER, status='proposed'):
        if row.subject_key not in current:
            row.status = 'withdrawn'
            row.save(update_fields=['status'])
            result['withdrawn'] += 1
    if found:
        items = [{'did': f'epic:{path}', 'campaign': campaign.name,
                  'comment': _comment(path, record, identity),
                  'files': record['files'], 'events': record['events'],
                  'registered_at': record['registered_at']}
                 for path, record, identity in found]
        outcome = _propose(items, proposer=PROPOSER, batch_id=batch_id,
                           created_by=created_by, rule=True)
        for key in ('proposed', 'noop', 'denied', 'invalid'):
            result[key] = len(outcome.get(key) or [])
    return result
