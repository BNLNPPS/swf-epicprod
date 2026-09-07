"""Content validation: the dataset reconciled against the production record.

The availability signal is only as sound as the dataset it names, so the
first validation is of the content itself, before any benchmark runs
(docs/EPICPROD_VALIDATION.md, Content validation). The production record
states which file is the delivered output of each work unit and how many
events it carries; the Rucio dataset states what it holds. Reconciling
the two names everything that would make the signal false:

  * a file in the dataset with no work unit behind it;
  * a work unit with no delivered file;
  * two files for one work unit;
  * a file whose event count was never recorded at registration, so any
    count for it would be inferred from its size rather than known.

This module reconciles and proposes. It writes nothing to Rucio and
detaches nothing: acceptance is an operator's single action, and it never
deletes — a detached file keeps its replica and expires under its own
lifetime, so a mistaken acceptance costs storage and not data.

The reconciliation reads JLab Rucio, so it runs in a doer and its result
is stored for pages to read. No page renders it live.
"""
import logging
from datetime import datetime, timezone as dt_timezone

logger = logging.getLogger(__name__)


def _dataset_files(dataset_did, scope='epic'):
    """The files the JLab dataset holds: {name: {bytes, events}}.

    Raises on an unreachable catalog: a reconciliation that cannot see the
    dataset must not report an empty one and call every work unit missing.
    """
    from .services import _jlab_rucio_auth, _jlab_rucio_get, _ndjson
    token = _jlab_rucio_auth()
    name = dataset_did.split(':', 1)[-1]
    text = _jlab_rucio_get(f'/dids/{scope}/{name}/files', token)
    files = {}
    for entry in _ndjson(text):
        if not isinstance(entry, dict):
            continue
        # JLab Rucio names a file by its path-like DID, not by a bare stem,
        # and the record holds the same form: compare them as they are.
        did = str(entry.get('name') or '')
        if not did:
            continue
        files[did] = {
            'bytes': entry.get('bytes'),
            'events': entry.get('events'),
        }
    return files


def reconcile(task, dataset_did=''):
    """What the dataset holds against what the record says was delivered.

    Returns the finding, which is a proposal and not an action. Every list
    is named by DID so an operator reads files rather than counts.
    """
    from .models import DeliveredOutput

    rows = list(DeliveredOutput.objects.filter(prod_task=task))
    delivered = [r for r in rows if r.status in ('delivered', 'diverted')]
    if dataset_did:
        delivered = [r for r in delivered
                     if _dataset_of([r]) == dataset_did]
    else:
        dataset_did = _dataset_of(delivered)
        delivered = [r for r in delivered
                     if _dataset_of([r]) == dataset_did]
    finding = {
        'task': task.composed_name or task.name,
        'dataset': dataset_did,
        'checked_at': datetime.now(dt_timezone.utc).isoformat(),
        'record_files': len(delivered),
        'units': 0,
        'matched': [],
        'orphan_files': [],
        'units_without_file': [],
        'units_with_several': [],
        'files_without_events': [],
        'delivered_events': 0,
        'error': '',
    }
    if not dataset_did:
        finding['error'] = ('the record names no registered output for this '
                            'task, so there is no dataset to reconcile')
        return finding
    try:
        held = _dataset_files(dataset_did)
    except Exception as e:                                    # noqa: BLE001
        logger.error('content validation: dataset %s unreadable: %s',
                     dataset_did, e)
        finding['error'] = f'the dataset could not be read: {e}'
        return finding

    by_unit = {}
    for row in delivered:
        by_unit.setdefault(row.segment or row.did, []).append(row)
    finding['units'] = len(by_unit)

    recorded_names = set()
    for unit, unit_rows in sorted(by_unit.items()):
        names = [r.registered_did or r.did for r in unit_rows]
        stems = names
        recorded_names.update(stems)
        present = [s for s in stems if s in held]
        if not present:
            finding['units_without_file'].append(
                {'unit': unit, 'expected': names})
            continue
        if len(present) > 1:
            # Which of the several the record calls the unit's delivered
            # output (status delivered, against diverted): acceptance keeps
            # that one and detaches the rest, or refuses when the record
            # names more than one.
            finding['units_with_several'].append(
                {'unit': unit, 'files': present,
                 'delivered': [s for r, s in zip(unit_rows, stems)
                               if s in held and r.status == 'delivered']})
        for row, stem in zip(unit_rows, stems):
            if stem not in held:
                continue
            recorded = held[stem].get('events')
            if recorded is None:
                # The count is what the availability signal is made of. A
                # file carrying none would have its count inferred from its
                # size, which is a guess wearing a number's clothes.
                finding['files_without_events'].append(
                    {'file': stem.rsplit('/', 1)[-1], 'did': stem,
                     'record_events': row.events})
                continue
            finding['matched'].append({'file': stem.rsplit('/', 1)[-1],
                                       'did': stem, 'events': int(recorded)})
            finding['delivered_events'] += int(recorded)

    for stem in sorted(held):
        if stem not in recorded_names:
            finding['orphan_files'].append(
                {'file': stem.rsplit('/', 1)[-1], 'did': stem,
                 'bytes': held[stem].get('bytes')})

    finding['sound'] = not (finding['orphan_files']
                            or finding['units_without_file']
                            or finding['units_with_several']
                            or finding['files_without_events']
                            or finding['error'])
    return finding


def _dataset_of(rows):
    """The dataset the record's delivered files sit in, from their DIDs."""
    for row in rows:
        did = row.registered_did or row.did
        if '/' in did:
            return did.rsplit('/', 1)[0]
    return ''


def datasets_of(task):
    """Every dataset the record names for this task, newest name last.

    A task's outputs span try namespaces — a rerun writes under its own
    prefix — so reconciliation is per dataset rather than per task, and a
    file in one try is not an orphan of another.
    """
    from .models import DeliveredOutput
    names = set()
    for row in DeliveredOutput.objects.filter(
            prod_task=task, status__in=('delivered', 'diverted')):
        dataset = _dataset_of([row])
        if dataset:
            names.add(dataset)
    return sorted(names)


def reconcile_all(task):
    """The finding for every dataset the record names for this task."""
    return [reconcile(task, dataset_did=name) for name in datasets_of(task)]


def acceptance_plan(finding):
    """What accepting this finding would do, or why it is refused.

    Acceptance is one action (docs/EPICPROD_VALIDATION.md, Content
    validation): files that do not belong are detached from the dataset,
    the delivered output of each work unit is affirmed, and the delivered
    event count becomes the sum of recorded counts. It never deletes.

    Two cases are refused rather than decided here. A file whose event
    count was never recorded cannot be counted, and the count is what the
    signal is made of, so the count is recorded first. A work unit with
    more than one file in delivered status is a genuine divergence, which
    is a person's decision (RUCIO_RESILIENCE.md, Measure 3).
    """
    plan = {'acceptable': False, 'refusals': [], 'detach': [],
            'affirm': 0, 'delivered_events': 0}
    if finding.get('error'):
        plan['refusals'].append(finding['error'])
    missing = finding.get('files_without_events') or []
    if missing:
        plan['refusals'].append(
            f'{len(missing)} file(s) carry no recorded event count; the '
            f'count is recorded before the content can be accepted')
    dataset = finding.get('dataset') or ''
    for orphan in finding.get('orphan_files') or []:
        plan['detach'].append(orphan.get('did') or f"{dataset}/{orphan['file']}")
    for unit in finding.get('units_with_several') or []:
        delivered = unit.get('delivered') or []
        if len(delivered) != 1:
            plan['refusals'].append(
                f"work unit {unit['unit']} has {len(delivered)} files in "
                f"delivered status; which is the output is a person's call")
            continue
        plan['detach'].extend(d for d in unit.get('files') or []
                              if d != delivered[0])
    detach = set(plan['detach'])
    kept = [m for m in finding.get('matched') or []
            if (m.get('did') or f"{dataset}/{m['file']}") not in detach]
    plan['affirm'] = len(kept)
    plan['delivered_events'] = sum(int(m.get('events') or 0) for m in kept)
    plan['acceptable'] = not plan['refusals']
    return plan


def stored_findings(task):
    """The findings the validation doer last stored for this task, with the
    acceptance plan of each, for pages. A read of the store and nothing
    else: no page reaches the catalog (CACHED_PRODUCTS.md)."""
    from monitor_app.models import CachedProduct
    key = f'content_validation:{task.composed_name or task.name}'
    row = CachedProduct.objects.filter(key=key).first()
    if not row or not row.value:
        return {'findings': [], 'built_at': None}
    findings = list((row.value or {}).get('findings') or [])
    for finding in findings:
        finding['plan'] = acceptance_plan(finding)
    return {'findings': findings,
            'built_at': (row.value or {}).get('built_at')
            or (row.built_at.isoformat() if row.built_at else None)}


def acceptances_of(task):
    """The acceptances recorded for this task's sample, by dataset: the
    stamp the record endpoint wrote on the dataset rows' metadata."""
    head = task.dataset
    if head is None:
        return {}
    return dict((head.get_metadata() or {}).get('content') or {})


def content_state(task):
    """What the compose page shows in its Content section: each dataset's
    stored finding with its plan, and the acceptance on record for it."""
    state = stored_findings(task)
    acceptances = acceptances_of(task)
    for finding in state['findings']:
        finding['acceptance'] = acceptances.get(finding.get('dataset') or '')
    state['accepted_events'] = sum(
        int(a.get('delivered_events') or 0) for a in acceptances.values())
    state['accepted_datasets'] = sorted(acceptances)
    return state
