"""ePIC container execution without the ATLAS bootstrap.

The pilot runs inside the campaign image the slot was asked for. A job for that
image executes in place. A job for another image runs nested in its own image
through the OSG pool's Apptainer on CVMFS, invoked by its supported wrapper
(which supplies its libraries); the outer container must carry a
/var/lib/apptainer directory with mnt/session, bound in by the submit
description (+SingularityBind), because inside the outer container the
relocatable runtime loses its own path and falls back to that location.
Nesting needs unprivileged user namespaces on the node.
"""
import logging
import os
import shlex
import shutil

logger = logging.getLogger(__name__)

# The OSG pool's Apptainer on CVMFS, through its wrapper; pinned to the version
# tested on 23 October 2026 (OSG_SUBMISSION.md, NATIVE_PILOT_CANARY.md).
OSG_APPTAINER = '/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/bin/apptainer'


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
    command = ('cd ' + q(workdir) + ' || exit 66; export TMPDIR=' + q(workdir)
               + ' GFORTRAN_TMPDIR=' + q(workdir) + ' PandaID=' + q(str(job.jobid))
               + '; ' + executable)
    job.command = command
    if actual and same_image(actual, wanted):
        logger.info('ePIC native: reuse matching image %s; no container launch', wanted)
        return command
    if actual:
        logger.warning('ePIC native: outer image %s is not the job image %s; '
                       'launching the job image nested', actual, wanted)
    launcher = runtime()
    if not launcher:
        raise RuntimeError('ePIC native: Apptainer/Singularity is unavailable for ' + wanted)
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
    args = [launcher, 'exec', '--no-home', '--bind', '/cvmfs:/cvmfs']
    for root in sorted(roots):
        args += ['--bind', root + ':' + root]
    args += ['--pwd', workdir, wanted, '/bin/bash', '-c', command]
    logger.info('ePIC native: launch one payload container %s', wanted)
    return shlex.join(args)
