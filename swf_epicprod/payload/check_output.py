#!/usr/bin/env python3
"""Whether this job's output already exists in the catalog of record.

Run by run.sh before any work, so a retry of a job whose earlier attempt
delivered its output skips the work, and a retry whose output name is
held by a failed earlier attempt stops in seconds instead of running for
an hour and failing at registration (epicprod payload,
swf-epicprod docs/EPICPROD_PAYLOAD.md).

Usage: check_output.py <scope> <did name> [expected events]

Asking whether a replica exists is not asking whether the data is right.
Where the job knows how many events it is to produce, the check requires
the registered DID to carry that count: a replica whose recorded count is
absent or different is not a delivered output, and the name is held by
something other than this job's work (docs/RUCIO_RESILIENCE.md,
Measure 3).

Prints one word on stdout:
  AVAILABLE  registered, an available replica, and the event count agrees
             (or no count was given to check against): delivered
  MISMATCH   registered with an available replica carrying a different
             event count, or none: the name holds other content
  HELD       registered with no available replica: a failed earlier
             attempt holds the name
  ABSENT     no such DID
Exit 0 on a definite answer; exit 3 when the catalog could not be read,
with the reason on stderr (the caller proceeds as if absent).
Reads Rucio with the job's credential and RUCIO_CONFIG, as registration does.
"""
import sys


def main():
    if len(sys.argv) not in (3, 4):
        print(f"usage: {sys.argv[0]} <scope> <did name> [expected events]",
              file=sys.stderr)
        return 2
    scope, name = sys.argv[1], sys.argv[2]
    expected = None
    if len(sys.argv) == 4 and str(sys.argv[3]).strip().isdigit():
        expected = int(sys.argv[3])
    try:
        from rucio.client import Client
        from rucio.common.exception import DataIdentifierNotFound
        client = Client()
        try:
            meta = client.get_did(scope, name)
        except DataIdentifierNotFound:
            print("ABSENT")
            return 0
        states = []
        for rep in client.list_replicas([{'scope': scope, 'name': name}],
                                        all_states=True):
            states.extend((rep.get('states') or {}).values())
        if "AVAILABLE" not in states:
            print("HELD")
            return 0
        if expected is None:
            print("AVAILABLE")
            return 0
        recorded = meta.get('events')
        if recorded is None:
            try:
                recorded = client.get_metadata(scope, name).get('events')
            except Exception as exc:  # noqa: BLE001
                print(f"event count unreadable: {exc}", file=sys.stderr)
                recorded = None
        if recorded is None:
            print("MISMATCH")
            print(f"{name} carries no event count; {expected} expected",
                  file=sys.stderr)
            return 0
        if int(recorded) != expected:
            print("MISMATCH")
            print(f"{name} carries {recorded} events; {expected} expected",
                  file=sys.stderr)
            return 0
        print("AVAILABLE")
        return 0
    except Exception as exc:  # noqa: BLE001 - the reason goes to stderr
        print(f"catalog read failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
