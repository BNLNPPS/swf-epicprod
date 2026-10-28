#!/usr/bin/env python3
"""Reconstruction once on each simulation output, from an Event Service job's
shipped record (swf_epicprod/es_records.py): every done unit reconstructed,
in full, by one pool request; no unit, event range or simulation output twice;
every pool request served accounted for by one unit; each close's events the
sum of its own units'. One JSON line a job; a clean job's lists are empty and
its two request counts equal.

    es-reco-once.py PANDAID [PANDAID ...]

Run with the deployed monitor's venv (as es-observe.py).
"""
import collections
import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location('es_observe', Path(__file__).with_name('es-observe.py'))
es_observe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es_observe)


def check(record):
    done, failed = record.get('done') or [], record.get('failed') or []
    flight = record.get('in_flight') or []
    ids = collections.Counter(u['unit_id'] for u in done + failed)

    def source_range(u):
        src = ((u.get('simulation') or {}).get('input_range') or {}).get('source_id')
        rng = u.get('range') or {}
        return (src, rng.get('startEvent'), rng.get('lastEvent'))

    ranges = collections.Counter(source_range(u) for u in done)
    outputs = collections.Counter((u.get('simulation') or {}).get('output_file') for u in done)
    # A pool member's request count against the units that name it: a
    # request no unit accounts for would be a reconstruction run twice.
    served, named = {}, collections.Counter()
    for u in done + failed + flight:
        rec = u.get('reconstruction') or {}
        member = rec.get('pool_member')
        if member is None:
            continue
        named[member] += 1
        served[member] = max(served.get(member, 0), int(rec.get('pool_requests') or 0))
    stood = {c['index']: c for c in record.get('closes') or [] if c.get('ok')}
    by_close = collections.Counter()
    for u in done:
        by_close[u.get('close')] += u.get('events') or 0
    return {
        'done': len(done), 'failed': len(failed), 'in_flight': len(flight),
        'unit_ids_twice': [k for k, v in ids.items() if v > 1][:5],
        'ranges_twice': [list(k) for k, v in ranges.items() if v > 1][:5],
        'outputs_twice': [k for k, v in outputs.items() if v > 1 and k][:5],
        'done_without_reco': [u['unit_id'] for u in done
                              if not (u.get('reconstruction') or {}).get('ended_at')][:5],
        'reco_events_short': [u['unit_id'] for u in done
                              if u.get('events_reconstructed') != u.get('events')][:5],
        'pool_requests_served': sum(served.values()), 'pool_requests_by_units': sum(named.values()),
        'members_over': {m: [served[m], named[m]] for m in served if served[m] > named[m]},
        'closes_stood': len(stood),
        'close_event_mismatch': {i: [c.get('events'), by_close.get(i)] for i, c in stood.items()
                                 if c.get('events') != by_close.get(i)},
        'done_without_stood_close': [u['unit_id'] for u in done if u.get('close') not in stood][:5],
        'failed_after_simulation': [[u['unit_id'], (u.get('message') or '')[:160]] for u in failed
                                    if (u.get('simulation') or {}).get('status') == 'completed'][:5],
    }


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    es_observe.deployed_environment()
    from monitor_app.payload_reports import sweeper_client
    from swf_epicprod import es_records
    client, bucket, prefix = sweeper_client()
    for pandaid in sys.argv[1:]:
        record = es_records.load(client, bucket, prefix, int(pandaid))
        print(json.dumps(dict(pandaid=int(pandaid), **check(record))))


if __name__ == '__main__':
    main()
