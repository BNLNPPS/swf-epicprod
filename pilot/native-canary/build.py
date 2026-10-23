#!/usr/bin/env python3
"""Build an ePIC native pilot from a verified deployed archive. Publishes nothing.

Without --production the archive is the isolated canary: it acquires one explicit
test task only. With --production that guard is left out and the archive serves
a queue's ordinary jobs; everything else is the same as the tested canary.
"""
import argparse
import ast
import gzip
import hashlib
import json
from pathlib import Path
import tarfile


def build(baseline, expected_sha256, output, production=False):
    digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ValueError('Baseline hash mismatch; refusing to patch a different pilot')
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(baseline) as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or path.parts[0] != 'pilot3':
                raise ValueError('Unsafe archive path: ' + member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError('Unexpected archive member type: ' + member.name)
        archive.extractall(output)
    tree = output / 'pilot3'
    changes = []

    def write(relative, text):
        path = tree / relative
        before = path.read_bytes() if path.exists() else b''
        ast.parse(text)
        path.write_text(text)
        changes.append({'path': relative, 'before': hashlib.sha256(before).hexdigest(),
                        'after': hashlib.sha256(path.read_bytes()).hexdigest()})

    write('pilot/user/epic/native_container.py',
          Path(__file__).with_name('native_container.py').read_text())
    relative = 'pilot/user/epic/container.py'
    content = (tree / relative).read_text()
    start = content.index('def wrapper(')
    end = content.index('\ndef fix_asetup(', start)
    write(relative, content[:start] + '''def wrapper(executable: str, **kwargs: Any) -> str:
    from pilot.user.epic.native_container import wrapper as native_wrapper
    return native_wrapper(executable, **kwargs)

''' + content[end:])

    # The node guard reaches a running pilot: before each further job the
    # pilot reads the guard's document the way the payload's landing check
    # does (one source, shipped as is) and takes no further job on an
    # excluded node. The first job is the launcher's pre-check.
    here = Path(__file__).resolve()
    landing = here.with_name('landing_check.py')  # beside the builder on a host
    if not landing.exists():
        landing = here.parents[2] / 'swf_epicprod/payload/landing_check.py'  # in the repository
    write('pilot/user/epic/landing_check.py', landing.read_text())
    relative = 'pilot/user/epic/common.py'
    content = (tree / relative).read_text()
    start = content.index('def allow_timefloor(')
    end = content.index('\n\n\ndef ', start)
    write(relative, content[:start] + '''def allow_timefloor(submitmode: str) -> bool:
    """ePIC: no further job on a node the node guard excludes.

    Read before each further job; the pilot then finishes the one it runs and
    exits ("timefloor is set to 0"). The document is the one the payload's
    landing check reads; a document that cannot be read allows the job, as
    the landing check then decides.
    """
    import json as _json
    import urllib.request as _request
    from datetime import datetime as _datetime, timezone as _timezone
    from pilot.user.epic import landing_check as guard
    try:
        with _request.urlopen(guard.EXCLUSION_URL, timeout=guard.EXCLUSION_TIMEOUT_S) as response:
            document = _json.loads(response.read().decode())
    except Exception as exc:  # noqa: BLE001
        logger.warning('node guard document not read (%s): the next job is allowed', exc)
        return True
    entry = guard.excluded_here(document, guard._host_names(),
                                os.environ.get('PILOT_SITENAME', '').strip(),
                                _datetime.now(_timezone.utc))
    if entry is not None:
        logger.warning('ePIC node guard: this node %s on %s is excluded since %s (%s); '
                       'no further job after the current one',
                       entry.get('host'), entry.get('queue'), entry.get('since'), entry.get('reason'))
        return False
    logger.info('ePIC node guard: this node is not excluded; next job allowed')
    return True''' + content[end:])

    relative = 'pilot/user/epic/utilities.py'
    content = (tree / relative).read_text()
    original = "        setup = get_asetup(asetup=False)\n        setup += 'lsetup prmon;'"
    if content.count(original) != 1:
        raise ValueError('Unexpected memory-monitor baseline')
    write(relative, content.replace(original, "        setup = 'command -v prmon >/dev/null || exit 69;'"))

    if not production:
        guard_test_task(tree, write)

    relative = 'pilot/copytool/rucio.py'
    content = (tree / relative).read_text()
    original = "    _file['no_register'] = True\n"
    if content.count(original) != 1:
        raise ValueError('Unexpected Rucio upload baseline')
    write(relative, content.replace(original, original + '''    # The queue's advertised pilot-log protocol, selected explicitly.
    # Keep payload output and other uploads unchanged.
    log_scheme = os.environ.get('EPICPROD_CANARY_LOG_SCHEME')
    if fspec.filetype == 'log' and log_scheme:
        _file['force_scheme'] = log_scheme
'''))

    # Authentication and refresh are deliberately carried from the baseline.
    with tarfile.open(baseline) as archive:
        assert archive.extractfile('pilot3/pilot/util/https.py').read() == (tree / 'pilot/util/https.py').read_bytes()
    packed = output / ('pilot3-native.tar.gz' if production else 'pilot3-native-canary.tar.gz')
    def metadata(member):
        if '__pycache__' in member.name or member.name.endswith('.pyc'):
            return None
        member.uid = member.gid = member.mtime = 0
        member.uname = member.gname = ''
        return member
    with packed.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                archive.add(tree, arcname='pilot3', filter=metadata)
    manifest = {'baseline': str(baseline.resolve()), 'baseline_sha256': digest,
                'candidate': packed.name, 'sha256': hashlib.sha256(packed.read_bytes()).hexdigest(),
                'changes': changes,
                'scope': ('production native pilot: the tested canary without its test guard'
                          if production else 'isolated canary; not a production release')}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def guard_test_task(tree, write):
    """The isolated canary acquires one explicit test task and nothing else."""
    relative = 'pilot/control/job.py'
    content = (tree / relative).read_text()
    fn = next(n for n in ast.parse(content).body if isinstance(n, ast.FunctionDef)
              and n.name == 'get_dispatcher_dictionary')
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    if len(returns) != 1:
        raise ValueError('Unexpected dispatcher baseline')
    lines = content.splitlines(keepends=True)
    lines.insert(returns[0].lineno - 1, '''    canary_task = int(os.environ['EPICPROD_CANARY_TASK_ID'])
    if canary_task <= 0 or args.job_label != 'test':
        raise RuntimeError('This isolated canary accepts only one explicit test task')
    data['task_id'] = canary_task
    data['prod_source_label'] = 'test'
''')
    write(relative, ''.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', required=True, type=Path, help='new directory; never overwritten')
    parser.add_argument('--production', action='store_true',
                        help='no test-task guard: the archive serves ordinary jobs')
    args = parser.parse_args()
    print(json.dumps(build(args.baseline, args.sha256, args.output, args.production), indent=2))
