#!/usr/bin/env python3
"""Find and remove duplicate Event Service events in a registered output dataset.

A unit whose ranges PanDA did not credit goes back to the task, and a later job
simulates its events again, so the dataset holds two files with the same event
IDs (EPICPROD_PAYLOAD.md, Resident Event Service units; GKE_PILOT_FLOW.md, the
ES batch channel). PanDA's credit decides which copy stands: an event ID
credited (JEDI_Events finished or done, 3 or 6) to the job that wrote a file stays in that file, and
the same ID in any other job's file is the duplicate. An ID that no job holds
credit for keeps its first copy, by file name.

  es-dedup.py plan   --dataset DID --task N --output PLAN.json      deployed venv (catalog, PanDA)
  es-dedup.py scan   --plan PLAN.json --output SCAN.json            campaign image (event IDs per file)
  es-dedup.py decide --plan PLAN.json --scan SCAN.json --output DECISION.json
  es-dedup.py skim   --decision DECISION.json --workdir DIR         campaign image (clean copies)
  es-dedup.py apply  --decision DECISION.json --workdir DIR --proxy FILE [--execute]   campaign image

A file with nothing to drop is left as it is. A file whose every event is
another copy's duplicate is detached from the dataset. A file holding both is
rewritten without the duplicates: its events tree copied through an entry list
and its other trees whole, the copy read back with the normal PODIO reader
(event count, the exact kept IDs, every collection), registered into the same
dataset at the same RSE through the payload's registrar with its event count,
and only then is the original detached. A detached file leaves the dataset's
rule, so the RSE's reaper removes its replica. The clean copy's name carries a
"d" after the close index (<stem>.<pandaid>.<close>d.eicrecon.edm4eic.root).
apply without --execute prints what it would do.

EventHeader IDs are zero-based; PanDA's job_processID is the one-based event
number, so an ID is job_processID - 1 (es-readback.py, the payload's
startEvent - 1 offset).
"""
import argparse
import json
import os
from pathlib import Path
import sys


def django_setup():
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    for raw in open('/opt/swf-monitor/config/env/production.env'):
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()


def job_of(name):
    """The PanDA job id in a close's file name, <stem>.<pandaid>.<close>.eicrecon.edm4eic.root."""
    return int(name.rsplit('/', 1)[-1].split('.')[-5])


def plan(args):
    django_setup()
    from django.db import connections
    from monitor_app.mcp.rucio import _load_catalog, _CATALOGS
    catalog = _load_catalog('es_output_dedup', _CATALOGS['jlab_rucio']['env'])
    scope, name = args.dataset.split(':', 1)
    listing = catalog.list_files(name=name, scope=scope, page=1, limit=1000)
    if listing.get('error'):
        raise RuntimeError(listing['error'])
    if (listing.get('pagination') or {}).get('has_more'):
        raise RuntimeError('The dataset listing is incomplete')
    files = listing.get('data', [])
    replicas = catalog.list_file_replicas(
        dids=[{'scope': f['scope'], 'name': f['name']} for f in files], page=1, limit=1000)
    if replicas.get('error'):
        raise RuntimeError(replicas['error'])
    by_did = {f"{r['scope']}:{r['name']}": r for r in replicas.get('data', [])}
    out = []
    for f in sorted(files, key=lambda f: f['name']):
        r = by_did.get(f"{f['scope']}:{f['name']}") or {}
        urls = [u for rse, paths in (r.get('rses') or {}).items()
                if (r.get('states') or {}).get(rse) in ('AVAILABLE', 'A') for u in paths if u.startswith('root://')]
        if not urls:
            raise RuntimeError('No available ROOT replica for ' + f['name'])
        out.append({'scope': f['scope'], 'name': f['name'], 'events': int(f.get('events') or 0),
                    'bytes': f.get('bytes'), 'pandaid': job_of(f['name']), 'url': urls[0],
                    'rses': sorted((r.get('rses') or {}).keys())})
    with connections['panda'].cursor() as cursor:
        cursor.execute('SELECT pandaid, job_processid FROM doma_panda.jedi_events '
                       'WHERE jeditaskid = %s AND status IN (3, 6)', [args.task])
        credit = {}
        for pandaid, process in cursor.fetchall():
            credit.setdefault(int(pandaid), []).append(int(process) - 1)
    result = {'dataset': args.dataset, 'task': args.task, 'files': out,
              'credited': {str(k): sorted(v) for k, v in credit.items()},
              'registered_events': sum(f['events'] for f in out),
              'credited_events': sum(len(v) for v in credit.values())}
    args.output.write_text(json.dumps(result) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('files', 'credited')} | {'files': len(out)}))


def scan(args):
    import ROOT
    manifest = json.loads(args.plan.read_text())
    out = {}
    for f in manifest['files']:
        handle = ROOT.TFile.Open(f['url'])
        if not handle or handle.IsZombie():
            raise RuntimeError('Cannot open ' + f['url'])
        tree = handle.Get('events')
        entries = int(tree.GetEntries())
        tree.SetEstimate(entries + 1)
        if tree.Draw('EventHeader.eventNumber', '', 'goff') != entries:
            raise RuntimeError(f"{f['name']}: one EventHeader per event expected")
        values = tree.GetV1()
        ids = [int(values[i]) for i in range(entries)]
        if entries != f['events']:
            raise RuntimeError(f"{f['name']}: {entries} events in the file, {f['events']} registered")
        out[f['name']] = ids
        handle.Close()
        print(f"{f['name'].rsplit('/', 1)[-1]}: {entries}", flush=True)
    args.output.write_text(json.dumps(out) + '\n')


def decide(args):
    manifest = json.loads(args.plan.read_text())
    ids_by_file = json.loads(args.scan.read_text())
    credited = {int(k): set(v) for k, v in manifest['credited'].items()}
    owner = {}
    for pandaid, ids in credited.items():
        for event in ids:
            if event in owner:
                raise RuntimeError(f'Event {event} credited to {owner[event]} and {pandaid}')
            owner[event] = pandaid
    seen, files = set(), []
    for f in manifest['files']:
        ids = ids_by_file[f['name']]
        keep, drop = [], []
        for entry, event in enumerate(ids):
            holder = owner.get(event)
            if holder is not None and holder != f['pandaid']:
                drop.append(entry)                      # another job's credited copy stands
            elif holder is None and event in seen:
                drop.append(entry)                      # no credit anywhere: the first copy stands
            elif event in seen and holder == f['pandaid']:
                drop.append(entry)                      # a repeat inside the owning job's files
            else:
                keep.append(entry)
                seen.add(event)
        action = 'keep' if not drop else 'remove' if not keep else 'rewrite'
        files.append(dict(f, action=action, keep_entries=keep, drop=len(drop), kept=len(keep)))
    kept = sum(f['kept'] for f in files)
    uncredited = sorted(set(e for ids in ids_by_file.values() for e in ids) - set(owner))
    summary = {'dataset': manifest['dataset'], 'files': len(files),
               'keep': sum(f['action'] == 'keep' for f in files),
               'rewrite': sum(f['action'] == 'rewrite' for f in files),
               'remove': sum(f['action'] == 'remove' for f in files),
               'registered_events': sum(len(v) for v in ids_by_file.values()),
               'events_after': kept, 'events_dropped': sum(f['drop'] for f in files),
               'credited_events': len(owner), 'present_without_credit': len(uncredited),
               'credited_but_absent': len(set(owner) - set(e for ids in ids_by_file.values() for e in ids))}
    args.output.write_text(json.dumps({'summary': summary, 'files': files}) + '\n')
    print(json.dumps(summary))


def clean_name(name):
    parts = name.split('.')
    parts[-4] += 'd'
    return '.'.join(parts)


def structure_ok(ROOT, target, url, kept):
    """The copy holds each of the source's objects once, every tree but
    events at the source's newest entry count, and events at the kept count."""
    source, copy = ROOT.TFile.Open(url), ROOT.TFile.Open(str(target))
    try:
        names = list(dict.fromkeys(key.GetName() for key in source.GetListOfKeys()))
        copied = [key.GetName() for key in copy.GetListOfKeys()]
        if sorted(copied) != sorted(names):
            return f'objects {sorted(copied)} against the source {sorted(names)}'
        for name in names:
            original = source.Get(name)
            if not original.InheritsFrom('TTree'):
                continue
            want = kept if name == 'events' else original.GetEntries()
            if copy.Get(name).GetEntries() != want:
                return f'{name}: {copy.Get(name).GetEntries()} entries, expected {want}'
        return None
    finally:
        source.Close()
        copy.Close()


def skim(args):
    import ROOT
    from podio import root_io
    decision = json.loads(args.decision.read_text())
    args.workdir.mkdir(parents=True, exist_ok=True)
    for f in decision['files']:
        if f['action'] != 'rewrite':
            continue
        target = args.workdir / clean_name(f['name']).rsplit('/', 1)[-1]
        done = args.workdir / (target.name + '.ok.json')
        if target.exists() and done.exists() and json.loads(done.read_text())['events'] == f['kept']:
            problem = structure_ok(ROOT, target, f['url'], f['kept'])
            if problem is None:
                continue
            print(f'{target.name}: rewriting, {problem}', flush=True)
            done.unlink()
        source = ROOT.TFile.Open(f['url'])
        if not source or source.IsZombie():
            raise RuntimeError('Cannot open ' + f['url'])
        out = ROOT.TFile(str(target) + '.tmp', 'RECREATE', '', source.GetCompressionSettings())
        # A file may keep older cycles of a tree beside the newest (ROOT's
        # backup of a tree written in steps); each object is copied once,
        # from its newest cycle, as a reader sees it.
        names = list(dict.fromkeys(key.GetName() for key in source.GetListOfKeys()))
        for name in names:
            obj = source.Get(name)
            if not obj.InheritsFrom('TTree'):
                out.cd()
                obj.Write(name)
                continue
            if name == 'events':
                entries = ROOT.TEntryList('keep', 'kept entries', obj)
                for entry in f['keep_entries']:
                    entries.Enter(entry)
                obj.SetEntryList(entries)
                out.cd()
                copy = obj.CopyTree('')
            else:
                out.cd()
                copy = obj.CloneTree(-1, 'fast')
            copy.Write(name, ROOT.TObject.kOverwrite)
        out.Close()
        source.Close()
        os.replace(str(target) + '.tmp', target)
        # The copy as a consumer reads it: the normal PODIO reader, every event,
        # every collection of the first, and exactly the kept identities.
        reader = root_io.Reader(str(target))
        ids, collections = [], None
        for frame in reader.get('events'):
            if collections is None:
                collections = sorted(frame.getAvailableCollections())
                for name in collections:
                    len(frame.get(name))
            ids.append(int(frame.get('EventHeader')[0].getEventNumber()))
        source = ROOT.TFile.Open(f['url'])
        tree = source.Get('events')
        tree.SetEstimate(tree.GetEntries() + 1)
        tree.Draw('EventHeader.eventNumber', '', 'goff')
        expected = [int(tree.GetV1()[i]) for i in f['keep_entries']]
        source.Close()
        if ids != expected or len(ids) != f['kept']:
            raise RuntimeError(f'{target.name}: read back {len(ids)} events, expected {f["kept"]}')
        problem = structure_ok(ROOT, target, f['url'], f['kept'])
        if problem is not None:
            raise RuntimeError(f'{target.name}: {problem}')
        (args.workdir / (target.name + '.ok.json')).write_text(json.dumps(
            {'events': len(ids), 'collections': len(collections or []), 'bytes': target.stat().st_size}) + '\n')
        print(f'{target.name}: {len(ids)} events, {len(collections or [])} collections', flush=True)


def apply(args):
    import shutil
    import subprocess
    import tempfile
    decision = json.loads(args.decision.read_text())
    scope, dataset = decision['summary']['dataset'].split(':', 1)
    payload = Path(__file__).resolve().parents[1] / 'swf_epicprod' / 'payload'
    proxy = Path(tempfile.mkdtemp()) / 'x509up'
    shutil.copy(args.proxy, proxy)
    proxy.chmod(0o600)
    env = dict(os.environ, X509_USER_PROXY=str(proxy), RUCIO_CONFIG=str(payload / 'rucio.cfg'),
               RUCIO_ACCOUNT='eicprod', REGISTRATION_STAGGER_MAX_S='0')
    os.environ.update(env)
    from rucio.client import Client
    client = Client()
    for f in decision['files']:
        if f['action'] == 'keep':
            continue
        if f['action'] == 'rewrite':
            local = args.workdir / clean_name(f['name']).rsplit('/', 1)[-1]
            ok = json.loads((args.workdir / (local.name + '.ok.json')).read_text())
            if ok['events'] != f['kept']:
                raise RuntimeError(f"{local.name}: the clean copy holds {ok['events']} events, the decision {f['kept']}")
            did = clean_name(f['name'])
            command = [sys.executable, str(payload / 'register_to_rucio.py'), '-f', str(local), '-d', did,
                       '-s', scope, '-r', 'BNL-XRD', '--events', str(ok['events']),
                       '--preserve-door', 'root://epicxrd1.sdcc.bnl.gov:1094', '--preserve-prefix', '/eic/EPIC']
            print(('register ' if args.execute else 'would register ') + f"{did} ({ok['events']} events)", flush=True)
            if args.execute:
                if subprocess.run(command, env=env).returncode != 0:
                    raise RuntimeError('Registration failed for ' + did)
                names = {x['name'] for x in client.list_files(scope, dataset)}
                if did.lstrip('/') not in {n.lstrip('/') for n in names}:
                    raise RuntimeError(did + ' is not attached to the dataset after registration')
        print(('detach ' if args.execute else 'would detach ') + f"{f['name']} ({f['drop']} duplicates of {f['events']})", flush=True)
        if args.execute:
            client.detach_dids(scope, dataset, [{'scope': f['scope'], 'name': f['name']}])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan'); p.add_argument('--dataset', required=True); p.add_argument('--task', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('scan'); p.add_argument('--plan', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('decide'); p.add_argument('--plan', type=Path, required=True); p.add_argument('--scan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('skim'); p.add_argument('--decision', type=Path, required=True); p.add_argument('--workdir', type=Path, required=True)
    p = sub.add_parser('apply'); p.add_argument('--decision', type=Path, required=True); p.add_argument('--workdir', type=Path, required=True)
    p.add_argument('--proxy', type=Path, required=True); p.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    {'plan': plan, 'scan': scan, 'decide': decide, 'skim': skim, 'apply': apply}[args.command](args)


if __name__ == '__main__':
    main()
