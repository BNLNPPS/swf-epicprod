#!/usr/bin/env python3
"""Build a test-only pilot from a verified deployed archive. Publishes nothing."""
import argparse
import ast
import gzip
import hashlib
import json
from pathlib import Path
import tarfile


def build(baseline, expected_sha256, output):
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

    relative = 'pilot/user/epic/utilities.py'
    content = (tree / relative).read_text()
    original = "        setup = get_asetup(asetup=False)\n        setup += 'lsetup prmon;'"
    if content.count(original) != 1:
        raise ValueError('Unexpected memory-monitor baseline')
    write(relative, content.replace(original, "        setup = 'command -v prmon >/dev/null || exit 69;'"))

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

    relative = 'pilot/copytool/rucio.py'
    content = (tree / relative).read_text()
    original = "    _file['no_register'] = True\n"
    if content.count(original) != 1:
        raise ValueError('Unexpected Rucio upload baseline')
    write(relative, content.replace(original, original + '''    # The canary explicitly selects the queue's advertised pilot-log protocol.
    # Keep payload output and other uploads unchanged.
    log_scheme = os.environ.get('EPICPROD_CANARY_LOG_SCHEME')
    if fspec.filetype == 'log' and log_scheme:
        _file['force_scheme'] = log_scheme
'''))

    # Authentication and refresh are deliberately carried from the baseline.
    with tarfile.open(baseline) as archive:
        assert archive.extractfile('pilot3/pilot/util/https.py').read() == (tree / 'pilot/util/https.py').read_bytes()
    packed = output / 'pilot3-native-canary.tar.gz'
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
                'changes': changes, 'scope': 'isolated canary; not a production release'}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', required=True, type=Path, help='new directory; never overwritten')
    args = parser.parse_args()
    print(json.dumps(build(args.baseline, args.sha256, args.output), indent=2))
