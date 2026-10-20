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
import json
import os
import ssl
import sys
import tempfile
import urllib.request

CRIC = "https://datalake-cric.cern.ch/api/atlas/pandaqueue/query/?json"
CAPATH = "/etc/grid-security/certificates"


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
    if len(args) != 2:
        sys.exit(__doc__)
    overrides_path, outfile = args
    with open(overrides_path) as f:
        overrides = json.load(f)
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
    outdir = os.path.dirname(os.path.abspath(outfile))
    os.makedirs(outdir, exist_ok=True)
    handle, path = tempfile.mkstemp(dir=outdir, suffix=".json")
    with os.fdopen(handle, "w") as out:
        json.dump(records, out)
    os.chmod(path, 0o644)
    os.replace(path, outfile)
    print(f"wrote {outfile}: {len(records)} queues; " + ("; ".join(changes) or "no override differs from CRIC"))


if __name__ == "__main__":
    main(sys.argv)
