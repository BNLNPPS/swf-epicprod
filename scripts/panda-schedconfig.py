#!/usr/bin/env python3
"""The PanDA server's queue records: CRIC's, with ours on top.

The server reads its queue records (schedconfig) from CRIC_URL_SCHEDCONFIG in
panda_server.cfg, which may be a URL or a local file (the configurator's
aux.get_dump), and refreshes its table from it every 200 s. Pointed at the
file this writes, every field the server acts on is ours to set
(docs/SCHEDCONFIG_OVERRIDES.md). This fetches CRIC's records, applies the
overrides file (queue -> {field: value}; keys starting with _ are notes kept
with the setting), and replaces the output file atomically. A queue CRIC does
not have is defined by `_clone`, the queue whose record it copies (taken after
that queue's own overrides, so it must come earlier in the file). When CRIC
cannot be read, or an override names a queue or field CRIC does not have, the
output is left as it is and the exit is nonzero, so the server keeps the last
good records.

Usage: panda-schedconfig.py OVERRIDES OUTFILE [--cric URL] [--capath DIR]
  e.g. panda-schedconfig.py pandaserver/schedconfig-overrides.json /opt/panda/etc/panda/schedconfig/schedconfig.json
CRIC's certificate is verified against the grid CA directory (default
/etc/grid-security/certificates, which carries the CERN CA).
"""

import copy
import datetime
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.request

CRIC = "https://datalake-cric.cern.ch/api/atlas/pandaqueue/query/?json"
CAPATH = "/etc/grid-security/certificates"
REPOSITORY = 'https://github.com/BNLNPPS/swf-epicprod.git'


def atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def repository_overrides():
    """Resolve main once, then fetch that immutable revision with TLS."""
    result = subprocess.run(['git', 'ls-remote', REPOSITORY, 'refs/heads/main'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=45, check=True)
    revision = result.stdout.split()[0]
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('invalid repository revision')
    url = ('https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/' + revision
           + '/pandaserver/schedconfig-overrides.json')
    with urllib.request.urlopen(url, timeout=45) as response:
        raw = response.read().decode('utf-8')
    return json.loads(raw), raw, revision


def _option(args, name, default):
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return default


def main(argv):
    args = argv[1:]
    cric = _option(args, "--cric", CRIC)
    capath = _option(args, "--capath", CAPATH)
    pilot_dir = _option(args, '--pilot-dir', None)
    sync = '--sync-from-github' in args
    if sync:
        args.remove('--sync-from-github')
    if len(args) != 2:
        sys.exit(__doc__)
    overrides_path, outfile = args
    try:
        if sync:
            overrides, raw_overrides, revision = repository_overrides()
        else:
            with open(overrides_path) as f:
                raw_overrides = f.read()
            overrides, revision = json.loads(raw_overrides), None
        if not isinstance(overrides, dict) or not overrides:
            raise ValueError('no overrides')
    except Exception as exc:
        sys.exit(f'panda-schedconfig: overrides not read ({exc}); {outfile} left as it is')
    try:
        context = ssl.create_default_context(capath=capath)
        with urllib.request.urlopen(cric, timeout=120, context=context) as response:
            records = json.load(response)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"panda-schedconfig: CRIC not read ({exc}); {outfile} left as it is")
    if not isinstance(records, dict) or not records:
        sys.exit(f"panda-schedconfig: CRIC returned no records; {outfile} left as it is")
    changes = []
    for queue, fields in overrides.items():
        record = records.get(queue)
        if record is None and "_clone" in fields:
            # A queue CRIC does not have: a copy of the record named, after
            # that record's own overrides, with this queue's fields on top.
            if fields["_clone"] not in records:
                sys.exit(f"panda-schedconfig: no queue {fields['_clone']} to clone for {queue}; {outfile} left as it is")
            record = records[queue] = copy.deepcopy(records[fields["_clone"]])
            changes.append(f"{queue}: new, cloned from {fields['_clone']}")
        if record is None:
            sys.exit(f"panda-schedconfig: CRIC has no queue {queue}; {outfile} left as it is")
        for field, value in fields.items():
            if field.startswith("_"):
                continue
            if field not in record:
                sys.exit(f"panda-schedconfig: {queue} has no field {field}; {outfile} left as it is")
            if record[field] != value and "_clone" not in fields:
                changes.append(f"{queue} {field}: {record[field]!r} -> {value!r}")
            record[field] = value
    output = json.dumps(records)
    atomic_write(outfile, output)
    if sync:
        atomic_write(overrides_path, raw_overrides)
    atomic_write(outfile + '.provenance.json', json.dumps({
        'repository': REPOSITORY, 'revision': revision,
        'overrides_sha256': hashlib.sha256(raw_overrides.encode()).hexdigest(),
        'output_sha256': hashlib.sha256(output.encode()).hexdigest(),
        'written_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }) + '\n')
    if pilot_dir:
        for queue in ('UM_GREX_PanDA_1', 'JLAB_PanDA_1'):
            atomic_write(os.path.join(pilot_dir, queue, 'queuedata.json'),
                         json.dumps(records[queue], indent=4) + '\n')
    print(f"wrote {outfile}: {len(records)} queues; " + ("; ".join(changes) or "no override differs from CRIC"))


if __name__ == "__main__":
    main(sys.argv)
