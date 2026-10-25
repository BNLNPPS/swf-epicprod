"""ePIC container execution without the ATLAS bootstrap.

The pilot runs inside the campaign image the slot was asked for. A job for that
image executes in place. A job for another image runs nested in its own image
through the OSG pool's Apptainer on CVMFS, invoked by its supported wrapper
(which supplies its libraries). Inside the outer container that relocatable
runtime loses its own path and falls back to /var/lib/apptainer/mnt/session,
which the image does not have and no glidein binds for a job; so the nested
launch runs in a private user and mount namespace of its own, with a tmpfs
over /var/lib carrying that directory. Nothing outside the launch sees it.
Nesting needs unprivileged user namespaces on the node.

Each job's payload gets a scratch of its own, ``epicprod-scratch-<PandaID>``
under the batch system's scratch, exported as ``_CONDOR_SCRATCH_DIR`` (which
the payload prefers to ``SLURM_TMPDIR``), and removed when the payload exits,
after the payload's own exit handling. A batch job runs several jobs, and a GREX
allocation eight pilots in one Slurm scratch; the payload removed only a scratch
it made itself, so each job's files stayed for the rest of the batch job
(EPICPROD_PAYLOAD.md, Cleaning up after each job). A payload the pilot kills
leaves its scratch behind: the next job of the same pilot sweeps it, from this
pilot's own ledger, never another pilot's. EPICPROD_KEEP_TMPDIR=1 keeps every
scratch for diagnosis. While the payload runs, its scratch's size and file count
are sampled every minute; the peaks go to ``epicprod-scratch-peak.txt`` in the
job directory, which the job metrics report (``scratchPeakMB``,
``scratchPeakFiles``). The PanDA job directory itself is never touched.
"""
import logging
import os
import shlex
import shutil

logger = logging.getLogger(__name__)

# The OSG pool's Apptainer on CVMFS, through its wrapper; pinned to the version
# tested on 23 October 2026 (OSG_SUBMISSION.md, NATIVE_PILOT_CANARY.md).
OSG_APPTAINER = '/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/bin/apptainer'


SESSION_DIR = '/var/lib/apptainer/mnt/session'

SCRATCH_PREFIX = 'epicprod-scratch-'
SCRATCH_LEDGER = '.epicprod-scratch-ledger'
PEAK_FILE = 'epicprod-scratch-peak.txt'
SAMPLE_S = 60


def scratch_base(workdir):
    """Where the batch system keeps a job's scratch: HTCondor's, Slurm's, else
    beside the pilot's job directories."""
    # A Condor-CE Slurm job can inherit its small shared CE spool as
    # _CONDOR_SCRATCH_DIR. Prefer the worker storage advertised by the site.
    slurm_job = os.environ.get('SLURM_JOB_ID', '')
    if slurm_job:
        for value in (os.environ.get('EPICPROD_WORKER_SCRATCH'), os.environ.get('SLURM_TMPDIR'),
                      '/scratch/slurm/' + slurm_job, os.environ.get('OSG_WN_TMP')):
            if value and os.path.isdir(value) and os.access(value, os.W_OK):
                return os.path.abspath(value)
        raise RuntimeError('Slurm worker has no writable site scratch; refusing CE spool fallback')
    for key in ('_CONDOR_SCRATCH_DIR', 'SLURM_TMPDIR'):
        value = os.environ.get(key, '')
        if value and os.path.isdir(value):
            return os.path.abspath(value)
    slurm = '/scratch/slurm/' + os.environ.get('SLURM_JOB_ID', '')
    if os.environ.get('SLURM_JOB_ID') and os.path.isdir(slurm):
        return slurm
    return os.path.dirname(os.path.abspath(workdir))


def disk_headroom(workdir):
    """Unprivileged available bytes/inodes, carried by the normal heartbeat."""
    import time
    values = {'diskSample': int(time.time())}
    paths = []
    ce = os.environ.get('EPICPROD_CE_SANDBOX')
    if ce:
        paths.append(('ce', ce))
    paths.append(('pilot', workdir))
    try:
        paths.append(('scratch', scratch_base(workdir)))
    except RuntimeError:
        pass  # Still report the pilot volume when no safe scratch exists.
    for label, path in paths:
        try:
            stat = os.statvfs(path)
        except OSError:
            continue
        values.update({label + 'FreeMB': stat.f_bavail * stat.f_frsize // 2**20,
                       label + 'TotalMB': stat.f_blocks * stat.f_frsize // 2**20,
                       label + 'IFree': stat.f_favail,
                       label + 'ITotal': stat.f_files})
    return values


def keep_scratch():
    return os.environ.get('EPICPROD_KEEP_TMPDIR', '') == '1'


def sweep(ledger, current):
    """Remove the scratch of this pilot's earlier jobs that is still there,
    from this pilot's ledger only, and record the current job's."""
    try:
        with open(ledger) as handle:
            earlier = handle.read().split()
    except OSError:
        earlier = []
    kept = []
    for path in earlier:
        if path == current or not os.path.basename(path).startswith(SCRATCH_PREFIX):
            continue
        if not os.path.isdir(path):
            continue
        if keep_scratch():
            kept.append(path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        logger.info('ePIC scratch: removed %s, left by an earlier job of this pilot', path)
    with open(ledger, 'w') as handle:
        handle.write('\n'.join(kept + [current]) + '\n')


def scratch_peak(workdir):
    """(KB, files) peaks the wrapper sampled for the job in ``workdir``, or None."""
    try:
        with open(os.path.join(workdir, PEAK_FILE)) as handle:
            parts = handle.read().split()
        kb, files = int(parts[0]), int(parts[1])
    except (OSError, ValueError, IndexError):
        return None
    return (kb, files) if kb > 0 else None


def scratch_command(executable, scratch, peak):
    """The payload run with its own scratch: sampled while it runs, removed
    after it has exited (unless EPICPROD_KEEP_TMPDIR=1), its exit code kept."""
    q = shlex.quote
    s, p = q(scratch), q(peak)
    sample = ('epicprod_peak() { k=$(du -skx ' + s + ' 2>/dev/null | cut -f1 || true); '
              'n=$(du -sx --inodes ' + s + ' 2>/dev/null | cut -f1 || true); '
              'pk=0; pn=0; [ -r ' + p + ' ] && read -r pk pn < ' + p + ' || true; '
              '[ "${k:-0}" -gt "${pk:-0}" ] 2>/dev/null && pk=$k; '
              '[ "${n:-0}" -gt "${pn:-0}" ] 2>/dev/null && pn=$n; '
              'echo "${pk:-0} ${pn:-0}" > ' + p + '.tmp && mv -f ' + p + '.tmp ' + p + ' || true; }; ')
    sampler = ('( trap \'kill $epicprod_sp 2>/dev/null; exit 0\' TERM; '
               'while :; do sleep ' + str(SAMPLE_S) + ' & epicprod_sp=$!; wait $epicprod_sp; epicprod_peak; done ) '
               '& epicprod_sampler=$!; ')
    remove = '' if keep_scratch() else 'rm -rf ' + s + '; '
    return ('export _CONDOR_SCRATCH_DIR=' + s + '; mkdir -p ' + s + ' || exit 66; ' + sample + sampler
            + '( ' + executable + '\n); epicprod_rc=$?; kill $epicprod_sampler 2>/dev/null; '
            'wait $epicprod_sampler 2>/dev/null; epicprod_peak; ' + remove + 'exit $epicprod_rc')


def nested(args):
    """The shell command for a nested launch: the runtime's own argv, inside
    a private namespace with the session directory when the outer container
    lacks it (osgsub01 and the pool's workers, 23 October 2026)."""
    launch = shlex.join(args)
    if os.path.isdir(SESSION_DIR):
        return launch
    inner = ('mount -t tmpfs -o size=1m,nodev,nosuid tmpfs /var/lib && mkdir -p ' + SESSION_DIR
             + ' && exec ' + launch)
    return shlex.join(['unshare', '--user', '--map-current-user', '--keep-caps', '--mount',
                       '--propagation', 'private', '/bin/bash', '-c', inner])


def runtime():
    if os.access(OSG_APPTAINER, os.X_OK):
        return OSG_APPTAINER
    return shutil.which('apptainer') or shutil.which('singularity')


def current_image():
    return os.environ.get('APPTAINER_CONTAINER') or os.environ.get('SINGULARITY_CONTAINER', '')


def same_image(actual, wanted):
    # Compare complete paths, never basenames or a guessed campaign version.
    return bool(actual and wanted and os.path.isabs(actual) and os.path.isabs(wanted)
                and os.path.realpath(actual) == os.path.realpath(wanted))


def wrapper(executable, **kwargs):
    job = kwargs.get('job')
    if job is None:
        raise RuntimeError('ePIC native container requires an explicit job and image')
    wanted = job.imagename
    if not wanted or not os.path.isabs(wanted):
        raise RuntimeError('ePIC canary requires an absolute, locally available job image')
    if not os.path.isdir(wanted):
        raise RuntimeError('ePIC job image is unavailable: ' + wanted)
    workdir = os.path.abspath(job.workdir)
    q = shlex.quote
    actual = current_image()
    options = getattr(job, 'containeroptions', {}) or {}
    if getattr(job, 'preprocess', None):
        payload = options.get('containerExec')
        if not payload:
            raise RuntimeError('ePIC multistep job has no containerExec')
        from pilot.user.epic.container import replace_last_command
        executable = replace_last_command(executable, payload)
    if options.get('execArgs'):
        executable += ' ' + options['execArgs']
    scratch = os.path.join(scratch_base(workdir), SCRATCH_PREFIX + str(job.jobid))
    sweep(os.path.join(os.path.dirname(workdir), SCRATCH_LEDGER), scratch)
    command = ('cd ' + q(workdir) + ' || exit 66; export TMPDIR=' + q(workdir)
               + ' GFORTRAN_TMPDIR=' + q(workdir) + ' PandaID=' + q(str(job.jobid))
               + '; ' + scratch_command(executable, scratch, os.path.join(workdir, PEAK_FILE)))
    job.command = command
    if actual and same_image(actual, wanted):
        logger.info('ePIC native: reuse matching image %s; no container launch', wanted)
        return command
    if actual and os.environ.get('EPICPROD_NATIVE_NESTING', '').lower() == 'off':
        raise RuntimeError('ePIC native (direct mode): this pilot runs image ' + actual
                           + ' and serves no other; the job wants ' + wanted + '. Refused before the payload.')
    if actual:
        logger.warning('ePIC native: outer image %s is not the job image %s; '
                       'launching the job image nested', actual, wanted)
    launcher = runtime()
    if not launcher:
        raise RuntimeError('ePIC native: Apptainer/Singularity is unavailable for ' + wanted)
    # Bind the whole pilot work tree, not an individual token file. Refresh
    # replaces token files; the payload must see the replacement through the
    # same directory and the same pathname.
    roots = {workdir, os.path.abspath(os.environ.get('PILOT_HOME', workdir)),
             scratch_base(workdir)}
    for key in ('PILOT_SOURCE_DIR', 'PILOT_WORK_DIR'):
        if os.environ.get(key):
            roots.add(os.path.abspath(os.environ[key]))
    for key in ('PANDA_AUTH_TOKEN', 'OIDC_AUTH_TOKEN', 'OIDC_REFRESHED_AUTH_TOKEN',
                'X509_USER_PROXY'):
        value = os.environ.get(key, '')
        if value and os.path.isabs(value):
            roots.add(os.path.dirname(value))
    args = [launcher, 'exec', '--no-home', '--bind', '/cvmfs:/cvmfs']
    for root in sorted(roots):
        args += ['--bind', root + ':' + root]
    args += ['--pwd', workdir, wanted, '/bin/bash', '-c', command]
    logger.info('ePIC native: launch one payload container %s', wanted)
    return nested(args)
