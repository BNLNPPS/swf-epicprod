"""Dataset definitions sweep — assimilate eic/simulation_campaign_datasets.

The production team defines datasets in the ``simulation_campaign_datasets``
repository: one seed CSV per dataset naming its EVGEN files, a GitLab CI
that measures each dataset's cost (real per-file event counts, per-event
walltime, per-event output sizes), and the background-mixing JSON configs
under ``config_data/``. This sweep assimilates all three onto the catalog:

- **Definitions inventory** — every definition with its EVGEN path tail,
  matched exactly against the registered EVGEN Rucio inventory (the
  ``evgen-rucio.json`` snapshot) and, with the request-side input matcher,
  against the catalog's evgen datasets. The three-way populations
  (defined / requested / registered, and the gaps) are the completeness
  signal.
- **Cost model** — each definition's CI timings artifact: number of files,
  real event counts, initialization and per-event walltime, per-event FULL
  and RECO output sizes. Fetched incrementally: a definition already
  costed in the previous snapshot is not re-fetched unless
  ``refresh_costs`` is set.
- **Background-config registry** — the ``config_data/*.json`` inventory,
  the valid ``BG_FILES`` values.

With ``apply=True`` the matched costs and definition references are written
to each catalog evgen dataset's ``metadata['definitions']``; the full
result is written as one snapshot JSON beside the Rucio snapshots. A step
of the nightly ``catalog_sync`` chain; also runnable standalone::

    python -m pcs.definitions_sweep [--apply] [--refresh-costs]

NO-SILENT-FAILURES: repository, fetch, and parse errors are recorded in
the summary's ``errors`` and surfaced in the printed JSON; they never
abort the remaining definitions.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import quote

REPO_DIR = os.environ.get(
    'SWF_DATASETS_REPO_DIR',
    '/data/wenauseic/github/simulation_campaign_datasets')
ARTIFACTS_BASEURL = os.environ.get(
    'SWF_DATASETS_ARTIFACTS_BASEURL',
    'https://eicweb.phy.anl.gov/api/v4/projects/491/jobs/artifacts/'
    '{tag}/raw/results/nightly/{detector_config}/main/datasets/timings/')
DATASET_TAG = os.environ.get('DATASET_TAG', 'main')
DETECTOR_CONFIG = os.environ.get('DETECTOR_CONFIG', 'epic_craterlake')
SNAPSHOT_NAME = 'dataset-definitions.json'
FETCH_TIMEOUT = 30
POPULATION_CAP = 100


def _git_refresh(repo_dir, errors):
    """Fast-forward the definitions clone; return the HEAD sha (stale or
    fresh — a pull failure is recorded, not fatal)."""
    pull = subprocess.run(['git', '-C', repo_dir, 'pull', '--ff-only', '-q'],
                          capture_output=True, text=True, timeout=120)
    if pull.returncode != 0:
        errors.append('git pull: '
                      + (pull.stderr or pull.stdout).strip().splitlines()[-1])
    head = subprocess.run(['git', '-C', repo_dir, 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, timeout=30)
    return head.stdout.strip() if head.returncode == 0 else ''


def _inventory(repo_dir, errors):
    """The definition CSVs: relative path, extension, and the EVGEN path
    tail (the seed row's file column is the Rucio tail's directory)."""
    defs = []
    for root, dirs, files in os.walk(repo_dir):
        rel_root = os.path.relpath(root, repo_dir)
        if rel_root.split(os.sep)[0] in ('.git', 'scripts', 'config_data'):
            dirs[:] = []
            continue
        for f in sorted(files):
            if not f.endswith('.csv'):
                continue
            rel = os.path.normpath(os.path.join(rel_root, f))
            try:
                first = open(os.path.join(root, f)).readline().strip()
                if ',' not in first and first.endswith('.csv'):
                    # An index file listing other definition CSVs (e.g.
                    # EXCLUSIVE/OMEGA.csv); its members are inventoried
                    # directly by the walk.
                    continue
                file_col, ext = first.split(',')[:2]
                tail = os.path.dirname(file_col).lower()
            except Exception as e:
                errors.append(f'parse {rel}: {e}')
                continue
            defs.append({'path': rel, 'ext': ext, 'tail': tail})
    defs.sort(key=lambda d: d['path'])
    return defs


def _bg_registry(repo_dir):
    bg_dir = os.path.join(repo_dir, 'config_data')
    if not os.path.isdir(bg_dir):
        return []
    return [{'file': 'config_data/' + f, 'name': os.path.splitext(f)[0]}
            for f in sorted(os.listdir(bg_dir)) if f.endswith('.json')]


def _fetch_cost(rel_path, errors):
    """One definition's CI timings artifact -> aggregate cost record.

    Artifact rows (``determine_timing.sh``): file, ext, nevents, init_s,
    per_event_s[, full_kb_init, full_kb_per_event, reco_kb_init,
    reco_kb_per_event] — the timing/size columns repeat per row; event
    counts are per file.
    """
    url = (ARTIFACTS_BASEURL.format(tag=DATASET_TAG,
                                    detector_config=DETECTOR_CONFIG)
           + quote(rel_path) + '?job=collect')
    try:
        raw = urllib.request.urlopen(url, timeout=FETCH_TIMEOUT).read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No timings artifact for this definition (the BACKGROUNDS
            # class does not run through the timings CI) — absent, not
            # an error.
            return 'absent'
        errors.append(f'cost fetch {rel_path}: {e}')
        return None
    except Exception as e:
        errors.append(f'cost fetch {rel_path}: {e}')
        return None
    rows = [r.split(',') for r in raw.strip().splitlines() if r.strip()]
    if not rows or len(rows[0]) < 5:
        errors.append(f'cost parse {rel_path}: unexpected artifact shape')
        return None
    try:
        cost = {
            'n_files': len(rows),
            'nevents_total': sum(int(r[2]) for r in rows),
            'nevents_per_file': [int(r[2]) for r in rows],
            'init_s': float(rows[0][3]),
            'per_event_s': float(rows[0][4]),
        }
        if len(rows[0]) >= 9:
            cost.update(full_kb_per_event=float(rows[0][6]),
                        reco_kb_per_event=float(rows[0][8]))
        return cost
    except (ValueError, IndexError) as e:
        errors.append(f'cost parse {rel_path}: {e}')
        return None


def sweep_dataset_definitions(*, apply=False, refresh_costs=False,
                              created_by='definitions_sweep'):
    """Run the sweep; return the summary dict (also snapshot-persisted)."""
    from django.conf import settings as _settings
    from .models import Dataset
    from .services import (RUCIO_SNAPSHOT_DIR, EVGEN_RUCIO_SNAPSHOT_NAME,
                           _evgen_input_match, _evgen_did_tail,
                           _request_input_tail)

    t0 = time.time()
    errors = []
    checked_at = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    head = _git_refresh(REPO_DIR, errors)
    definitions = _inventory(REPO_DIR, errors)
    bg_configs = _bg_registry(REPO_DIR)

    snap_path = os.path.join(RUCIO_SNAPSHOT_DIR, SNAPSHOT_NAME)
    previous_costs = {}
    if os.path.exists(snap_path) and not refresh_costs:
        try:
            for d in json.load(open(snap_path)).get('definitions', []):
                if d.get('cost'):
                    previous_costs[d['path']] = d['cost']
        except Exception as e:
            errors.append(f'previous snapshot read: {e}')

    for d in definitions:
        cost = previous_costs.get(d['path']) or _fetch_cost(d['path'], errors)
        if cost == 'absent':
            d['cost'], d['cost_status'] = None, 'absent'
        elif cost:
            d['cost'], d['cost_status'] = cost, 'ok'
        else:
            d['cost'], d['cost_status'] = None, 'error'

    # Registered side: the EVGEN Rucio snapshot (exact tail match).
    rucio_tails = {}
    evgen_snap = os.path.join(RUCIO_SNAPSHOT_DIR, EVGEN_RUCIO_SNAPSHOT_NAME)
    try:
        for rec in json.load(open(evgen_snap)).get('datasets', []):
            rucio_tails[_evgen_did_tail(rec['did'])] = rec['did']
    except Exception as e:
        errors.append(f'evgen rucio snapshot read {evgen_snap}: {e}')
    for d in definitions:
        d['registered'] = d['tail'] in rucio_tails

    # Requested side: catalog evgen datasets through the input matcher.
    requested_tails = set()
    ds_matches = {}
    evgen_rows = list(Dataset.objects.filter(metadata__stage='evgen'))
    for ds in evgen_rows:
        req_tail = _request_input_tail(ds.source_location or '')
        if not req_tail:
            continue
        matched = [d for d in definitions
                   if _evgen_input_match(req_tail, d['tail'])]
        if matched:
            ds_matches[ds.pk] = matched
            requested_tails.update(d['tail'] for d in matched)
    for d in definitions:
        d['requested'] = d['tail'] in requested_tails

    populations = {
        'defined': len(definitions),
        'registered': sum(1 for d in definitions if d['registered']),
        'requested': sum(1 for d in definitions if d['requested']),
        'defined_not_registered': [d['path'] for d in definitions
                                   if not d['registered']][:POPULATION_CAP],
        'defined_not_requested': [d['path'] for d in definitions
                                  if not d['requested']][:POPULATION_CAP],
        'registered_not_defined': sorted(
            did for tail, did in rucio_tails.items()
            if not any(d['tail'] == tail for d in definitions)
        )[:POPULATION_CAP],
    }

    applied = 0
    if apply:
        for ds in evgen_rows:
            matched = ds_matches.get(ds.pk)
            if not matched:
                continue
            meta = ds.metadata or {}
            meta['definitions'] = {
                'checked_at': checked_at,
                'matched': [{'path': d['path'], 'tail': d['tail'],
                             'registered': d['registered'],
                             'cost': d['cost']} for d in matched],
            }
            ds.metadata = meta
            ds.save(update_fields=['metadata'])
            applied += 1

    summary = {
        'created_by': created_by,
        'checked_at': checked_at,
        'repo_head': head,
        'definitions': len(definitions),
        'with_cost': sum(1 for d in definitions if d['cost']),
        'cost_absent': sum(1 for d in definitions
                           if d.get('cost_status') == 'absent'),
        'bg_configs': len(bg_configs),
        'rucio_evgen_datasets': len(rucio_tails),
        'catalog_evgen_datasets': len(evgen_rows),
        'datasets_matched': len(ds_matches),
        'applied': applied if apply else 0,
        'populations': {k: (v if isinstance(v, int) else len(v))
                        for k, v in populations.items()},
        'errors': errors,
        'duration_s': round(time.time() - t0, 1),
    }

    snapshot = {'summary': summary, 'definitions': definitions,
                'bg_configs': bg_configs, 'populations': populations}
    try:
        os.makedirs(RUCIO_SNAPSHOT_DIR, exist_ok=True)
        with open(snap_path, 'w') as f:
            json.dump(snapshot, f, indent=1)
    except OSError as e:
        summary['errors'].append(f'snapshot write {snap_path}: {e}')
    return summary


def main():
    import argparse
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE',
                          'swf_monitor_project.settings')
    django.setup()
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='write matched definitions/costs to the catalog')
    parser.add_argument('--refresh-costs', action='store_true',
                        help='re-fetch every cost artifact')
    parser.add_argument('--created-by', default='definitions_sweep')
    args = parser.parse_args()
    summary = sweep_dataset_definitions(
        apply=args.apply, refresh_costs=args.refresh_costs,
        created_by=args.created_by)
    print(json.dumps(summary))
    return 1 if summary['errors'] and not summary['definitions'] else 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
