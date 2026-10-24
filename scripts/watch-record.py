#!/usr/bin/env python3
"""watch-record.py: the watcher's outcome and promise records
(EPICPROD_WATCHER.md, Items and outcomes).

    watch-record.py outcome KEY act  --note 'paused 41039, reported red (log 6458xxx)'
    watch-record.py outcome KEY push --note 'recommend X; answer wanted: yes/no'
    watch-record.py outcome KEY wait --until 2026-10-24T08:00Z --note 'Fir glideins return'
    watch-record.py promise --until 2026-10-24T06:00Z --note 'verify 26692.1 logs getjob_requests=30'

KEY is the item key the tick's digest names. A push reaches the
operator's Capcom feed; record it after putting the question to him.

Environment: SWF_MONITOR_HTTP_URL and SWF_API_TOKEN (source ~/.env).
"""
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('prod_watch', os.path.join(HERE, 'prod-watch.py'))
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='what', required=True)
    o = sub.add_parser('outcome')
    o.add_argument('key')
    o.add_argument('kind', choices=('act', 'push', 'wait'))
    o.add_argument('--note', required=True)
    o.add_argument('--until')
    o.add_argument('--by', default=os.environ.get('TJAI_SESSION_NAME', 'swf-watch'))
    p = sub.add_parser('promise')
    p.add_argument('--until', required=True)
    p.add_argument('--note', required=True)
    p.add_argument('--by', default=os.environ.get('TJAI_SESSION_NAME', 'swf-watch'))
    a = ap.parse_args(argv)
    if a.what == 'outcome':
        if a.kind == 'wait' and not a.until:
            ap.error('a wait needs --until')
        msg = f'{a.key}: {a.kind}: {a.note}' + (f' (until {a.until})' if a.until else '')
        rec = pw.post_log('watch_outcome', msg, subject_key=a.key, reason=a.kind,
                          extra={'kind': a.kind, 'until': a.until, 'by': a.by, 'username': a.by})
    else:
        msg = f'promise by {a.by}, due {a.until}: {a.note}'
        rec = pw.post_log('watch_promise', msg, extra={'until': a.until, 'by': a.by, 'username': a.by})
    print(f"record {rec.get('id', '?')}: {msg}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
