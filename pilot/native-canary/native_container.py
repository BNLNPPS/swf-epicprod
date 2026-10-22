"""ePIC canary container execution, with no ATLAS bootstrap or nested launch."""
import logging
import os
import shlex
import shutil

logger = logging.getLogger(__name__)


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
    command = ('cd ' + q(workdir) + ' || exit 66; export TMPDIR=' + q(workdir)
               + ' GFORTRAN_TMPDIR=' + q(workdir) + ' PandaID=' + q(str(job.jobid))
               + '; ' + executable)
    job.command = command
    if actual:
        if not same_image(actual, wanted):
            raise RuntimeError('ePIC refuses a nested container or a mismatched image: '
                               + actual + ' != ' + wanted)
        logger.info('ePIC native: reuse matching image %s; no container launch', wanted)
        return command
    runtime = shutil.which('apptainer') or shutil.which('singularity')
    if not runtime:
        raise RuntimeError('ePIC native: Apptainer/Singularity is unavailable')
    # Bind the whole pilot work tree, not an individual token file. Refresh
    # replaces token files; the payload must see the replacement through the
    # same directory and the same pathname.
    roots = {workdir, os.path.abspath(os.environ.get('PILOT_HOME', workdir))}
    for key in ('PILOT_SOURCE_DIR', 'PILOT_WORK_DIR'):
        if os.environ.get(key):
            roots.add(os.path.abspath(os.environ[key]))
    for key in ('PANDA_AUTH_TOKEN', 'OIDC_AUTH_TOKEN', 'OIDC_REFRESHED_AUTH_TOKEN',
                'X509_USER_PROXY'):
        value = os.environ.get(key, '')
        if value and os.path.isabs(value):
            roots.add(os.path.dirname(value))
    args = [runtime, 'exec', '--no-home', '--bind', '/cvmfs:/cvmfs']
    for root in sorted(roots):
        args += ['--bind', root + ':' + root]
    args += ['--pwd', workdir, wanted, '/bin/bash', '-c', command]
    logger.info('ePIC native: launch one payload container %s', wanted)
    return shlex.join(args)
