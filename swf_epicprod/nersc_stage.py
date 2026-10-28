"""EVGEN input staged ahead at NERSC for Event Service production on
Perlmutter (docs/NERSC_PERLMUTTER.md, EVGEN input in production).

A task's EVGEN files are first registered at BNL_PROD_DISK_1
(swf_epicprod/evgen_es.py). This module copies them from there onto the
community file system, ``CFS_DIR``, ahead of the task's jobs, so a job reads
its file at NERSC (the harness's ``ES_INPUT_DIRS``) and no job pulls one over
the WAN when it starts. The copy is a Slurm job submitted through the IRI API
(swf_epicprod/nersc_iri.py) under its account, which reads BNL with our
production Rucio proxy kept privately at NERSC (Torre Wenaus, 2026-10-28:
"put the rucio proxy there"): ``PRIVATE_DIR`` in that account's home, mode
700, the proxy mode 600, replaced whenever ours is newer.

Each file lands as ``CFS_DIR/<lfn>``: written as a hidden part file, checked
against the catalog's adler32, then renamed in its directory. A file present
at its catalog size is not copied again.
"""
import os
import shlex
import time

from .nersc_iri import Client

CFS_DIR = '/global/cfs/cdirs/m3763/epicprod/esinput'
PRIVATE_DIR = '/global/homes/w/wguan/epicprod'
PROXY = PRIVATE_DIR + '/rucio-proxy'
JOB_DIR = PRIVATE_DIR + '/transfers'
ACCOUNT = 'm3763'
QOS = 'xfer'
# Generous for the largest task (p4499: eight files, 28.6 GB) at tens of MB/s.
DURATION_S = 6 * 3600


def ensure_proxy(client, local):
    """Our proxy at NERSC, private: replaced when ours is newer or differs
    in size. Returns the NERSC copy's stat record."""
    data = open(local, 'rb').read()
    if client.stat(PRIVATE_DIR) is None:
        client.mkdir(PRIVATE_DIR)
    client.chmod(PRIVATE_DIR, '700')
    there = client.stat(PROXY)
    if there is None or there['size'] != len(data) or os.path.getmtime(local) > there['mtime']:
        client.upload(PROXY, data)
    client.chmod(PROXY, '600')
    there = client.stat(PROXY)
    if there is None or there['size'] != len(data) or int(str(there['mode'])[-3:]) != 600:
        raise RuntimeError(f'the proxy at NERSC is not in place privately: {there}')
    if client.stat(JOB_DIR) is None:
        client.mkdir(JOB_DIR)
    return there


def job_spec(name, script, duration_s=DURATION_S):
    """A one-core job in the transfer QOS running ``script`` under bash, its
    output in ``JOB_DIR``."""
    return {
        'name': name,
        'executable': '/bin/bash',
        'arguments': ['-c', script],
        'directory': JOB_DIR,
        'stdout_path': f'{JOB_DIR}/{name}.out',
        'stderr_path': f'{JOB_DIR}/{name}.err',
        'resources': {'node_count': 1, 'process_count': 1},
        # The transfer QOS runs on the cron architecture; the API's compute
        # resource asks for cpu unless told otherwise (sbatch refuses that).
        'attributes': {'duration': duration_s, 'queue_name': QOS, 'account': ACCOUNT,
                       'custom_attributes': {'constraint': 'cron'}},
    }


PROBE = ('date -u; hostname; id -un; '
         'for d in /cvmfs/atlas.cern.ch /cvmfs/grid.cern.ch /cvmfs/singularity.opensciencegrid.org '
         '/etc/grid-security/certificates ' + CFS_DIR.rsplit('/', 1)[0] + '; do '
         'if [ -d "$d" ]; then echo "dir $d"; else echo "no $d"; fi; done; '
         'for t in xrdcp curl python3 apptainer; do echo "$t: $(command -v $t || echo none)"; done')


def bnl_urls(proxy, lfns):
    """{lfn: root URL at BNL_PROD_DISK_1}: the RSE offers only xrootd."""
    import tempfile
    from pathlib import Path
    from .evgen_es import BNL_RSE, BNL_SCOPE, STAGE_DIR, _catalogs
    # GSI refuses the group-readable service proxy: a private copy, as
    # evgen_es.stage makes.
    with tempfile.TemporaryDirectory(prefix='nersc-stage-proxy-', dir=STAGE_DIR) as temp:
        private = Path(temp) / 'proxy'
        private.write_bytes(Path(proxy).read_bytes())
        private.chmod(0o600)
        bnl, _ = _catalogs(str(private))
        dids = [f'{BNL_SCOPE}:{lfn}' for lfn in lfns]
        urls = bnl.lfns2pfns(BNL_RSE, dids, operation='read', scheme='root')
    return {did.split(':', 1)[1]: url for did, url in urls.items()}


# Metadata timing on the two shared filesystems a job writes: N small files
# created, renamed in place, flocked and removed in /pscratch and in CFS, each
# operation timed (Python on the node; flock as reco_pool.acquire takes it).
METADATA_PROBE = r'''python3 - <<'PY'
import fcntl, os, statistics, time
for base in (os.path.expandvars('$PSCRATCH/epicprod-mdprobe'), '/global/cfs/cdirs/m3763/epicprod/mdprobe'):
    os.makedirs(base, exist_ok=True)
    t = {'create': [], 'rename': [], 'flock': [], 'unlink': []}
    for i in range(40):
        p = os.path.join(base, f'f{i}.tmp')
        a = time.time(); open(p, 'w').write('x'); t['create'].append(time.time() - a)
        a = time.time(); os.replace(p, p[:-4]); t['rename'].append(time.time() - a)
        a = time.time(); s = open(p[:-4], 'a'); fcntl.flock(s, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(s, fcntl.LOCK_UN); s.close(); t['flock'].append(time.time() - a)
        a = time.time(); os.unlink(p[:-4]); t['unlink'].append(time.time() - a)
    print(base, {k: (round(statistics.median(v) * 1000, 1), round(max(v) * 1000, 1)) for k, v in t.items()}, 'ms median, max')
    os.rmdir(base)
PY'''


# Bulk write and removal on the two shared filesystems: 2 GB written,
# synced, then removed, each timed.
IO_PROBE = r'''for base in "$PSCRATCH/epicprod-ioprobe" /global/cfs/cdirs/m3763/epicprod/ioprobe; do
  mkdir -p "$base"; f="$base/blob"
  a=$(date +%s.%N); dd if=/dev/zero of="$f" bs=4M count=512 status=none; b=$(date +%s.%N)
  sync "$f"; c=$(date +%s.%N); rm -f "$f"; d=$(date +%s.%N)
  echo "$base write $(echo "$b - $a" | bc) s, sync $(echo "$c - $b" | bc) s, rm $(echo "$d - $c" | bc) s"
  dd if=/dev/zero of="$f" bs=4M count=512 status=none; e=$(date +%s.%N); rm -f "$f"; g=$(date +%s.%N)
  echo "$base rm unsynced 2 GB: $(echo "$g - $e" | bc) s"
  rmdir "$base"
done'''


# Scratch quota of the harvester's account (xin, whose work directories the
# jobs fill) beside this account's.
QUOTA_PROBE = ('for u in xin wguan; do echo "== $u"; lfs quota -h -u $u /pscratch 2>&1 | tail -3; done; '
               'echo "== group m3763"; lfs quota -h -g m3763 /pscratch 2>&1 | tail -3')


def missing(client, files):
    """The staged files (``evgen_es.stage``) not at NERSC at their catalog size."""
    out = []
    for f in files:
        there = client.stat(f'{CFS_DIR}/{f["lfn"]}')
        if there is None or int(there['size']) != int(f['bytes']):
            out.append(f)
    return out


# A transfer node (probe 2026-10-28, login20) has CVMFS but no xrdcp of its
# own and no grid CA directory; BNL_PROD_DISK_1 serves only xrootd. xrdcp
# comes from ATLAS's tools on CVMFS, as the site's wrapper takes it.
SETUP = ('export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase\n'
         'source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" --quiet >/dev/null 2>&1\n'
         'lsetup -q xrootd >/dev/null 2>&1\n'
         'export X509_CERT_DIR=/cvmfs/grid.cern.ch/etc/grid-security/certificates\n'
         'command -v xrdcp >/dev/null || { echo "FAILED no xrdcp"; exit 2; }\n'
         'xrdcp --version 2>&1 | head -1')
COPIER = 'xrdcp --nopbar --force --cksum "adler32:$3" "$1" "$2"'


def copy_script(files, sources, copier):
    """The transfer job's script: each file to a hidden part file, checked
    against its adler32, then renamed into place in ``CFS_DIR``. ``sources``
    maps an LFN to its BNL URL; ``copier`` is the copy command line taking
    the source and the target, and the adler32 checksum as ``$3``."""
    lines = [SETUP, 'set -u', f'export X509_USER_PROXY={shlex.quote(PROXY)}',
             f'mkdir -p {shlex.quote(CFS_DIR)}', 'failed=0',
             'copy() { ' + copier + '; }']
    for f in files:
        target = f'{CFS_DIR}/{f["lfn"]}'
        part = f'{CFS_DIR}/.{f["lfn"]}.part'
        lines.append(
            f'if copy {shlex.quote(sources[f["lfn"]])} {shlex.quote(part)} {shlex.quote(f["adler32"])}'
            f' && mv -f {shlex.quote(part)} {shlex.quote(target)}; then echo "ok {f["lfn"]}";'
            f' else echo "FAILED {f["lfn"]}"; rm -f {shlex.quote(part)}; failed=1; fi')
    lines.append('exit $failed')
    return '\n'.join(lines)


def wait_job(client, job_id, timeout_s, poll_s=15):
    deadline = time.time() + timeout_s
    while True:
        try:
            state = client.job(job_id)
        except (OSError, RuntimeError) as exc:
            # A slow or failed status answer is not the job's state.
            if time.time() > deadline:
                return {'status': f'unknown: {exc}'}
            time.sleep(poll_s)
            continue
        status = str((state.get('status') or {}).get('state') if isinstance(state.get('status'), dict)
                     else state.get('status') or state.get('state') or '').lower()
        if status in ('completed', 'failed', 'canceled', 'cancelled', 'timeout', 'done') \
                or time.time() > deadline:
            return state
        time.sleep(poll_s)
