#!/usr/bin/env python3
"""One job of an analysis run (swf-epicprod docs/ANALYSIS.md, The job).

    analysis_job.py <seq> <csv base>

The job's manifest row is line <seq> of <csv base>.csv in the sandbox.
ANALYSIS_STAGE says which kind of job this is:

map    row seq;label;job;did;did;...  Fetch the analysis repository at the
       pinned commit, check its card against the registered digest, resolve
       the DIDs to xrootd URLs, write them where the card's files.list
       says, run the card's map stage, and upload the partial
       (files.partial) unregistered to the run's scratch.
merge  row k;label;merge;job=did;...  Fetch the repository, put every
       partial of the sample back where files.partial says, run the merge
       and finalize stages, and publish each plot with its sidecar, the
       result card, to the run's campaign folder.

Every job writes analysis-report.json and, when job reporting is
configured, sends it out of the worker with report_out.py, whence the
operations agent posts it to PCS. Only analysis tasks run this program;
production jobs never call it.

ANALYSIS_LOCAL=1 runs the same steps with no Rucio, storage or report:
DIDs resolve under ANALYSIS_URL_PREFIX, and the run's scratch and
publish folders are directories under ANALYSIS_LOCAL_STORE. That is how
a card is tried outside PanDA.

Standard library only, except the Rucio client the image carries.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get('ANALYSIS_REPORT', 'analysis-report.json')
SCHEMA = 'epicprod-analysis-report/1'
READ_RSES = [r for r in os.environ.get('ANALYSIS_READ_RSES', 'BNL-XRD,EIC-XRD').split(',') if r]
# EIC-XRD-LOG: where an unregistered upload with DID name /X lands, and the
# door and token base to read it back (docs/ANALYSIS.md, Storage).
STORE_DOOR = os.environ.get('ANALYSIS_STORE_DOOR', 'root://dtn-rucio.jlab.org:1094')
STORE_PREFIX = os.environ.get('ANALYSIS_STORE_PREFIX', '/work/eic3/EPIC/eic')
STORE_TOKEN_BASE = os.environ.get('ANALYSIS_STORE_TOKEN_BASE', '/work/eic3/EPIC')
LOCAL = os.environ.get('ANALYSIS_LOCAL') == '1'


class JobError(RuntimeError):
    pass


def log(msg):
    print(f'analysis: {msg}', flush=True)


def env(name):
    value = os.environ.get(name, '')
    if not value:
        raise JobError(f'{name} is not set')
    return value


def run(cmd, cwd=None, extra_env=None, shell=False):
    log(f'run: {cmd if shell else " ".join(cmd)}')
    result = subprocess.run(cmd, cwd=cwd, shell=shell, executable='/bin/bash' if shell else None,
                            env=dict(os.environ, **(extra_env or {})))
    if result.returncode != 0:
        raise JobError(f'command exited {result.returncode}: {cmd if shell else cmd[0]}')


# --- the analysis repository ----------------------------------------------

def fetch_repo(dest):
    """The repository at the pinned commit, without LFS content (a stage
    pulls what it needs), and its card checked against the tag's digest:
    the job runs the card that was registered or nothing."""
    repo, commit = env('ANALYSIS_REPO'), env('ANALYSIS_COMMIT')
    os.makedirs(dest, exist_ok=True)
    quiet = {'GIT_LFS_SKIP_SMUDGE': '1', 'GIT_TERMINAL_PROMPT': '0'}
    run(['git', 'init', '-q', dest], extra_env=quiet)
    run(['git', '-C', dest, 'fetch', '-q', '--depth', '1', repo, commit], extra_env=quiet)
    run(['git', '-C', dest, 'checkout', '-q', 'FETCH_HEAD'], extra_env=quiet)
    card_path = os.path.join(dest, env('ANALYSIS_CARD'))
    with open(card_path, 'rb') as f:
        text = f.read()
    digest = hashlib.sha256(text).hexdigest()
    if digest != env('ANALYSIS_CARD_SHA256'):
        raise JobError(f'{env("ANALYSIS_CARD")} at {commit[:12]} has sha256 {digest}, '
                       f'not the registered {env("ANALYSIS_CARD_SHA256")}')
    import yaml  # the image's; json would do for a JSON card
    return yaml.safe_load(text)


def stage_env(sample, job):
    """The variables a stage command and the card's paths may use: $SAMPLE,
    $JOB, $PARAMS and every parameter by name."""
    params = os.environ.get('ANALYSIS_PARAMS', '')
    values = dict(w.split('=', 1) for w in params.split())
    values.update({'SAMPLE': sample, 'JOB': job, 'PARAMS': params})
    return values


def substitute(template, values):
    def one(m):
        name = m.group(1) or m.group(2)
        if name not in values:
            raise JobError(f'{template!r} uses ${name}, which a job does not set')
        return values[name]
    return re.sub(r'\$\{(\w+)\}|\$(\w+)', one, template)


def run_stage(card, stage, repo_dir, values):
    run(card['stages'][stage], cwd=repo_dir, extra_env=values, shell=True)


# --- data --------------------------------------------------------------------

def resolve(dids):
    """xrootd URLs for the DIDs, preferring READ_RSES in order."""
    if LOCAL:
        prefix = env('ANALYSIS_URL_PREFIX').rstrip('/')
        return [f'{prefix}/{d.lstrip("/")}' for d in dids]
    from rucio.client import Client
    client = Client()
    wanted = [{'scope': 'epic', 'name': d} for d in dids]
    urls = {}
    for rep in client.list_replicas(wanted, schemes=['root'],
                                    rse_expression='|'.join(READ_RSES)):
        for rse in READ_RSES:
            pfns = (rep.get('rses') or {}).get(rse)
            if pfns:
                urls[rep['name']] = pfns[0]
                break
    missing = [d for d in dids if d not in urls]
    if missing:
        raise JobError(f'{len(missing)} file(s) with no replica at {"/".join(READ_RSES)}, '
                       f'first {missing[0]}')
    return [urls[d] for d in dids]


def adler32(path):
    value = 1
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            value = zlib.adler32(chunk, value)
    return f'{value & 0xffffffff:08x}'


def upload(path, did):
    """Write a file unregistered to the run's area on EIC-XRD-LOG (or the
    local store)."""
    if LOCAL:
        dest = os.path.join(env('ANALYSIS_LOCAL_STORE'), did.lstrip('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(path, dest)
        return
    run([sys.executable, os.path.join(HERE, 'register_to_rucio.py'), '-f', path, '-d', did,
         '-s', 'epic', '-r', os.environ.get('ANALYSIS_RSE', 'EIC-XRD-LOG'), '--noregister'])


def download(did, dest):
    """Read back a file the run wrote unregistered, with a storage read
    token for the run's scratch folder."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    if LOCAL:
        shutil.copyfile(os.path.join(env('ANALYSIS_LOCAL_STORE'), did.lstrip('/')), dest)
        return
    path = f'{STORE_PREFIX}{did}'
    folder = path.rsplit('/', 1)[0]
    token = subprocess.run(
        [sys.executable, os.path.join(HERE, 'tokens.py'), 'storage', '--door', STORE_DOOR,
         '--path', folder, '--base-path', STORE_TOKEN_BASE, '--verb', 'storage.read'],
        capture_output=True, text=True)
    extra = {}
    if token.returncode == 0 and token.stdout.strip():
        extra = {'XrdSecPROTOCOL': 'ztn', 'BEARER_TOKEN': token.stdout.strip()}
    else:
        log(f'no storage read token ({token.stderr.strip()[-200:]}); reading with the job\'s credential')
    run(['xrdcp', '--force', '--nopbar', f'{STORE_DOOR}/{path}', dest], extra_env=extra)


# --- the two kinds of job --------------------------------------------------

def map_job(row, repo_dir, report):
    seq, label, job, *dids = row.split(';')
    card = fetch_repo(repo_dir)
    values = stage_env(label, job)
    urls = resolve(dids)
    lst = os.path.join(repo_dir, substitute(card['files']['list'], values))
    os.makedirs(os.path.dirname(lst), exist_ok=True)
    with open(lst, 'w') as f:
        f.write('\n'.join(urls) + '\n')
    log(f'{len(urls)} file(s) of {label} for job {job}')
    run_stage(card, 'map', repo_dir, values)
    partial = os.path.join(repo_dir, substitute(card['files']['partial'], values))
    if not os.path.isfile(partial):
        raise JobError(f'the map stage left no {card["files"]["partial"]}')
    did = f'{env("ANALYSIS_SCRATCH")}/{label}.job_{job}.root'
    upload(partial, did)
    report['outputs'] = [{'seq': int(seq), 'did': did, 'adler32': adler32(partial),
                          'bytes': os.path.getsize(partial), 'files_read': len(urls),
                          'status': 'written'}]


def merge_job(row, repo_dir, report):
    _, label, _, *parts = row.split(';')
    partials = dict(p.split('=', 1) for p in parts)
    card = fetch_repo(repo_dir)
    # Lists first, partials after: a partial newer than its list is up to
    # date to a make-like runner, which must not redo a map job here.
    for job in partials:
        lst = os.path.join(repo_dir, substitute(card['files']['list'], stage_env(label, job)))
        os.makedirs(os.path.dirname(lst), exist_ok=True)
        open(lst, 'w').close()
    jobs = json.load(open('analysis_jobs.json'))
    by_did = {j['did']: j for j in jobs}
    for job, did in sorted(partials.items()):
        dest = os.path.join(repo_dir, substitute(card['files']['partial'], stage_env(label, job)))
        download(did, dest)
        want = (by_did.get(did) or {}).get('adler32')
        if want and adler32(dest) != want:
            raise JobError(f'{did}: adler32 {adler32(dest)}, the map job reported {want}')
    values = stage_env(label, '')
    run_stage(card, 'merge', repo_dir, values)
    run_stage(card, 'finalize', repo_dir, values)
    hists = json.load(open(os.path.join(repo_dir, substitute(card['files']['hists'], values))))
    plot_dir = os.path.join(repo_dir, substitute(card['files']['plots'], values))
    plots = sorted(p for p in os.listdir(plot_dir) if p.endswith('.png'))
    if not plots:
        raise JobError(f'finalize left no plot in {card["files"]["plots"]}')
    result = result_card(card, label, jobs, hists)
    published = []
    for plot in plots:
        name = f'{label}.{plot}'
        sidecar = dict(result, plot=name)
        side_path = os.path.join(plot_dir, name[:-4] + '.json')
        with open(side_path, 'w') as f:
            json.dump(sidecar, f, indent=1, sort_keys=True)
        upload(os.path.join(plot_dir, plot), f'{env("ANALYSIS_PUBLISH")}/{name}')
        upload(side_path, f'{env("ANALYSIS_PUBLISH")}/{name[:-4]}.json')
        published.append(name)
    result['plots'] = published
    report['result'] = result


def result_card(card, label, jobs, hists):
    """The sidecar: the analysis card, the run card and run record, this
    sample's jobs, and a digest of every histogram's bin contents."""
    run_card = json.load(open('analysis_run_card.json'))
    record = json.load(open('analysis_run.json'))
    mine = [j for j in jobs if _label(j['sample']) == label]
    digests = {name: {'entries': h['entries'],
                      'bins_sha256': hashlib.sha256(json.dumps(
                          h['bins'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
               for name, h in sorted(hists.items())}
    return {
        'schema': 1,
        'identifier': card.get('identifier'),
        'description': card.get('description'),
        'eic_shell': card.get('eic_shell'),
        'analysis_card': card,
        'run_id': record['run_id'],
        'run_card': run_card,
        'purpose': record['purpose'],
        'owner': record['owner'],
        'campaign': record['campaign'],
        'sample': label,
        'jobs': mine,
        'files_read': sum(j.get('files') or 0 for j in mine),
        'histograms': digests,
        'panda_tasks': record.get('panda_tasks'),
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _label(sample):
    return '_'.join(sample.strip('/').split('/')[-3:])


def main():
    seq, csv_base = int(sys.argv[1]), sys.argv[2]
    stage = env('ANALYSIS_STAGE')
    report = {'schema': SCHEMA, 'stage': stage, 'run_id': os.environ.get('ANALYSIS_RUN_ID'),
              'seq': seq, 'pandaid': os.environ.get('PandaID'), 'exit': 1}
    try:
        with open(f'{csv_base}.csv') as f:
            rows = f.read().splitlines()
        if not 0 <= seq < len(rows):
            raise JobError(f'no row {seq} in {csv_base}.csv ({len(rows)} rows)')
        repo_dir = os.path.abspath('analysis-repo')
        {'map': map_job, 'merge': merge_job}[stage](rows[seq], repo_dir, report)
        report['exit'] = 0
    except (JobError, KeyError, OSError, ValueError) as e:
        report['error'] = str(e)
        log(f'FAILED: {e}')
    finally:
        with open(REPORT, 'w') as f:
            json.dump(report, f, indent=1)
        send_report(report)
    return report['exit']


def send_report(report):
    """Out of the worker, when job reporting is configured; never fails
    the job (report_out.py)."""
    if LOCAL or not os.environ.get('REPORT_OUT_BUCKET'):
        return
    rid = (report.get('run_id') or 'unknown')[:12]
    key = f"analysis/{rid}/{report['stage']}_{report['seq']}.json"
    subprocess.run([sys.executable, os.path.join(HERE, 'report_out.py'),
                    '--file', REPORT, '--key', key])


if __name__ == '__main__':
    sys.exit(main())
