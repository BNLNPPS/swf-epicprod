#!/usr/bin/env python3
"""Pilot-side queue data for one PanDA queue: the CRIC record with local settings.

The pilot wrapper reads a queuedata.json in the pilot's working directory
before the CRIC cache (docs/OSG_SUBMISSION.md, Queue behavior without a CRIC
edit), so a copy shipped with each pilot sets pilot-side fields for that
queue alone. This writes that copy from the server's current record, with the
given fields replaced, to OUTDIR/queuedata.json. Run it again when the CRIC
record changes.

Usage: pilot-queuedata.py QUEUE OUTDIR FIELD=VALUE [FIELD=VALUE ...]
  e.g. pilot-queuedata.py UM_GREX_PanDA_1 /data/atlpan/harvester_common/queuedata/UM_GREX_PanDA_1 timefloor=180 maxtime=18000
Values are integers. timefloor is in minutes and maxtime in seconds, as in CRIC.
"""

import json
import os
import sys
import tempfile
import urllib.request

CACHE = "http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/{queue}.all.json"


def main(argv):
    if len(argv) < 4:
        sys.exit(__doc__)
    queue, outdir, settings = argv[1], argv[2], argv[3:]
    with urllib.request.urlopen(CACHE.format(queue=queue), timeout=30) as response:
        record = json.load(response)
    if record.get("panda_queue", record.get("nickname")) != queue:
        sys.exit(f"pilot-queuedata: the cache returned no record for {queue}")
    for setting in settings:
        field, _, value = setting.partition("=")
        if field not in record:
            sys.exit(f"pilot-queuedata: {queue} has no field {field}")
        print(f"{field}: {record[field]} -> {int(value)}")
        record[field] = int(value)
    os.makedirs(outdir, exist_ok=True)
    handle, path = tempfile.mkstemp(dir=outdir, suffix=".json")
    with os.fdopen(handle, "w") as out:
        json.dump(record, out)
    os.chmod(path, 0o644)
    os.replace(path, os.path.join(outdir, "queuedata.json"))
    print(f"wrote {os.path.join(outdir, 'queuedata.json')}")


if __name__ == "__main__":
    main(sys.argv)
