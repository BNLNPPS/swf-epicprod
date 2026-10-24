#!/usr/bin/env python3
"""Find a detector in the ePIC Preliminary Design Report
(docs/EPIC_DETECTOR_REFERENCE.md).

    pdr-lookup.py DRICH                 the section for a DD4hep detector or a
                                        system name: printed and PDF pages,
                                        .tex source, its parts
    pdr-lookup.py --list                every system and its detectors
    pdr-lookup.py --grep 'aerogel'      where a term appears in the report's
                                        source, with the heading it sits under
    pdr-lookup.py DRICH --text [PART]   the section's text from the PDF
                                        (pdftotext), or one part of it:
                                        Requirements, Justification, ...

Reads docs/epic-detector-reference.json, built by scripts/pdr-index.py.
Standard library only, plus pdftotext for --text.
"""
import argparse
import json
import os
import re
import subprocess
import sys

INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs',
                     'epic-detector-reference.json')


def load(path):
    with open(path) as fh:
        return json.load(fh)


def norm(text):
    return re.sub(r'[^a-z0-9]+', '', text.lower())


def resolve(index, query):
    """The system a query names: a DD4hep detector name (case-insensitive,
    also a prefix such as 'ForwardRomanPot'), a system name, or a word of a
    section title."""
    q = norm(query)
    dets = index['detectors']
    exact = [n for n in dets if norm(n) == q]
    if not exact:
        exact = [n for n in dets if norm(n).startswith(q)]
    if exact:
        systems = {dets[n]['system'] for n in exact}
        return [s for s in index['systems'] if s['system'] in systems], exact
    hits = [s for s in index['systems']
            if q in norm(s['system']) or (s['section'] and q in norm(s['section']['title']))]
    if hits:
        return hits, []
    topics = [t for t in index['topics'] if t['section'] and q in norm(t['topic'])]
    return [{'system': t['topic'], 'section': t['section'], 'requirements': None,
             'detectors': []} for t in topics], []


def show(index, system, matched):
    sec = system['section']
    print(system['system'])
    if matched:
        for name in matched:
            d = index['detectors'][name]
            print(f"  detector  {name}  ({d['type']}, {d['compact']})")
    if system['detectors'] and not matched:
        print('  detectors ' + ', '.join(system['detectors']))
    if sec:
        print(f"  section   {sec['title']}: p. {sec['page']}-{sec['page_end']} "
              f"(PDF {sec['pdf_page']}-{sec['pdf_end']})")
        if sec.get('tex'):
            print(f"  source    {index['report']['source']}/{sec['tex']}")
        for part in sec.get('parts', []):
            print(f"    {part['title']:<28} p. {part['page']:<5} (PDF {part['pdf_page']})"
                  f"  {part.get('tex', '')}")
    req = system.get('requirements')
    if req:
        print(f"  targets   {req['title']}: p. {req['page']} (PDF {req['pdf_page']})")
    print(f"  report    {index['report']['pdf']}")


def text(index, system, part):
    sec = system['section']
    first, last = sec['pdf_page'], sec['pdf_end']
    if part:
        parts = sec.get('parts', [])
        names = [norm(p['title']) for p in parts]
        try:
            i = next(k for k, n in enumerate(names) if n.startswith(norm(part)))
        except StopIteration:
            sys.exit(f"no part {part!r}; parts: {', '.join(p['title'] for p in parts)}")
        first = parts[i]['pdf_page']
        last = parts[i + 1]['pdf_page'] if i + 1 < len(parts) else sec['pdf_end']
    subprocess.run(['pdftotext', '-layout', '-f', str(first), '-l', str(last),
                    index['report']['pdf'], '-'], check=True)


def grep(index, term, limit):
    root = index['report']['source']
    headings, marks = index['headings'], index['marks']
    pages = {}
    for row in index['outline']:
        if row.get('tex'):
            pages[row['tex']] = row
    pat = re.compile(term, re.I)
    count = 0
    for rel, file_marks in marks.items():
        path = os.path.join(root, rel)
        try:
            lines = open(path, errors='replace').read().splitlines()
        except OSError:
            continue
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith('%') or not pat.search(line):
                continue
            h = -1
            for start, idx in file_marks:
                if start <= no:
                    h = idx
            title, where = '', ''
            if h >= 0:
                head = headings[h]
                title = head['title']
                row = pages.get(f"{head['file']}:{head['line']}")
                where = f"p. {row['page']}" if row else ''
            print(f"{rel}:{no}  [{title}{', ' + where if where else ''}]  "
                  f"{line.strip()[:120]}")
            count += 1
            if count >= limit:
                print(f'... stopped at {limit}; narrow the term or raise --limit')
                return


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('query', nargs='?')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--grep')
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--text', nargs='?', const='', default=None, metavar='PART')
    ap.add_argument('--index', default=INDEX)
    args = ap.parse_args()
    index = load(args.index)
    if args.list:
        for s in index['systems']:
            sec = s['section'] or {}
            print(f"{s['system']:<40} p. {sec.get('page', '-'):<4} "
                  f"{', '.join(s['detectors'])}")
        return
    if args.grep:
        grep(index, args.grep, args.limit)
        return
    if not args.query:
        ap.error('name a detector or system, or use --list or --grep')
    systems, matched = resolve(index, args.query)
    if not systems:
        sys.exit(f"nothing for {args.query!r}; try --list or --grep")
    if args.text is not None:
        text(index, systems[0], args.text)
        return
    for s in systems:
        show(index, s, matched)


if __name__ == '__main__':
    main()
