#!/usr/bin/env python3
"""Whether this job's output already exists in the catalog of record.

Run by run.sh before any work, so a retry of a job whose earlier attempt
delivered its output skips the work, and a retry whose output name is
held by a failed earlier attempt stops in seconds instead of running for
an hour and failing at registration (epicprod payload,
swf-epicprod docs/EPICPROD_PAYLOAD.md).

Usage: check_output.py <scope> <did name>

Prints one word on stdout:
  AVAILABLE  the DID is registered with an available replica: delivered
  HELD       the DID is registered with no available replica: a failed
             earlier attempt holds the name
  ABSENT     no such DID
Exit 0 on a definite answer; exit 3 when the catalog could not be read,
with the reason on stderr (the caller proceeds as if absent).
Reads Rucio with the job's credential and RUCIO_CONFIG, as registration does.
"""
import sys


def main():
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <scope> <did name>", file=sys.stderr)
        return 2
    scope, name = sys.argv[1], sys.argv[2]
    try:
        from rucio.client import Client
        from rucio.common.exception import DataIdentifierNotFound
        client = Client()
        try:
            client.get_did(scope, name)
        except DataIdentifierNotFound:
            print("ABSENT")
            return 0
        states = []
        for rep in client.list_replicas([{'scope': scope, 'name': name}],
                                        all_states=True):
            states.extend((rep.get('states') or {}).values())
        print("AVAILABLE" if "AVAILABLE" in states else "HELD")
        return 0
    except Exception as exc:  # noqa: BLE001 - the reason goes to stderr
        print(f"catalog read failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
