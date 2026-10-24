#!/usr/bin/env python3
"""Build the ePIC detector reference index (docs/EPIC_DETECTOR_REFERENCE.md).

The ePIC Preliminary Design Report is the place to go to learn about a
detector, say one that is blowing up reconstruction; this index is the way
in, not a copy (Torre Wenaus, 2026-10-20: "a focused targeted ref center in
the repo"). It joins three sources:

- the report's PDF outline, for printed page labels and PDF page numbers of
  every heading down to the Requirements / Justification / Implementation
  level;
- the report's LaTeX source, for the .tex file and line behind each heading
  and the files a heading's text lives in;
- the DD4hep geometry of the campaign image, for every top-level detector
  name production sees (the names in findings, crash signatures and the
  computing-centre model), each mapped to the report section that describes
  it through the SYSTEMS table below.

Usage:
    python3 scripts/pdr-index.py [--pdr DIR] [--geometry DIR] [--out FILE]
    python3 scripts/pdr-index.py --markdown   # the table for the doc

Needs pypdf (python3 -m pip install --user pypdf). Rerun when the report or
the geometry release changes; the lookup tool (scripts/pdr-lookup.py) reads
only the JSON it writes.
"""
import argparse
import json
import os
import re
import sys
from datetime import date

PDR_DIR = '/data/wenauseic/epic-pdr'
PDF_NAME = 'ePIC_Preliminary_Design_Report_3.1.pdf'
IMAGE = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable'
GEOMETRY_RELEASE = '26.07.3'
CONFIG = 'epic_craterlake.xml'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs',
                   'epic-detector-reference.json')

# Detector systems: the report heading that describes each (as its PDF
# outline titles it, under Experimental Systems unless noted), the Detector
# Requirements heading that sets its targets, and the DD4hep detector names
# (regular expressions, matched whole) of the 26.07.3 craterlake geometry
# that belong to it.
SYSTEMS = [
    ('Magnet', 'Magnet', None,
     [r'Solenoid(Barrel|EndcapN|EndcapP)']),
    ('Silicon trackers (SVT)', 'The silicon trackers',
     'Requirements of Tracking and Momentum Measurement in the Central Detector',
     [r'VertexBarrel(SubAssembly)?', r'SVT_IB_Support_.*', r'SagittaSiBarrel',
      r'OuterSiBarrel', r'(Inner|Middle|Outer)SiBarrelSubAssembly',
      r'InnerSiTrackerSubAssembly', r'(Middle|Outer)SiEndcapSubAssembly',
      r'(Inner|Middle|Outer)TrackerEndcap[NP]', r'InnerTrackerSupport']),
    ('MPGD trackers', 'The MPGD trackers',
     'Requirements of Tracking and Momentum Measurement in the Central Detector',
     [r'InnerMPGDBarrel(SubAssembly)?', r'MPGDOuterBarrel', r'OuterBarrelMPGDSubAssembly', r'(Backward|Forward)MPGD',
      r'EndcapMPGDSubAssembly']),
    ('Time of flight (TOF)', 'The time-of-flight layers', 'Particle Identification (PID)',
     [r'(Barrel|Backward|Forward)TOF', r'(Barrel|Endcap)TOFSubAssembly']),
    ('pfRICH', 'The proximity focusing RICH', 'Particle Identification (PID)',
     [r'PFRICH']),
    ('hpDIRC', 'The high performance DIRC', 'Particle Identification (PID)',
     [r'cb_DIRC']),
    ('dRICH', 'The dual radiator RICH', 'Particle Identification (PID)',
     [r'DRICH']),
    ('Backward EMCal (EEEMCal)', 'The backward endcap electromagnetic calorimeter',
     'Electromagnetic Calorimetry', [r'EcalEndcapN']),
    ('Barrel EMCal (BIC)', 'The barrel electromagnetic calorimeter',
     'Electromagnetic Calorimetry',
     [r'EcalBarrel(Imaging|ScFi|TrackerSubAssembly)']),
    ('Forward EMCal (pEMCal)', 'The forward endcap electromagnetic calorimeter',
     'Electromagnetic Calorimetry', [r'EcalEndcapP']),
    ('Backward HCal (nHCal)', 'The backward endcap hadronic calorimeter',
     'Hadronic Calorimetry', [r'HcalEndcapN', r'FluxEndcapN']),
    ('Barrel HCal (BHCal)', 'The barrel hadronic calorimeter', 'Hadronic Calorimetry',
     [r'HcalBarrel', r'FluxBarrel']),
    ('Forward HCal (LFHCAL and insert)', 'The forward endcap hadronic calorimeter',
     'Hadronic Calorimetry', [r'LFHCAL', r'HcalEndcapPInsert', r'FluxEndcapP']),
    ('Roman pots and off-momentum detectors', 'The roman pots and the off-momentum detectors',
     'Far-Forward and Far-Backward Detectors',
     [r'ForwardRomanPot_Station_\d+', r'ForwardOffMTracker_station_\d+']),
    ('B0 detectors', 'B0 detectors in the B0 magnet', 'Far-Forward and Far-Backward Detectors',
     [r'B0Tracker(Companion|SubAssembly)?', r'B0ECal']),
    ('Zero Degree Calorimeter (ZDC)', 'The Zero Degree Calorimeter',
     'Far-Forward and Far-Backward Detectors',
     [r'ZDC_Crystal', r'HcalFarForwardZDC_SiPMonTile']),
    ('Luminosity system', 'The luminosity system', 'Far-Forward and Far-Backward Detectors',
     [r'Lumi.*', r'AnalyzerMag', r'SweeperMag']),
    ('Low-Q2 taggers', 'The low-Q2 taggers', 'Far-Forward and Far-Backward Detectors',
     [r'BackwardsTagger(Station|Vacuum)']),
    ('Beam pipe, beamline magnets and vacuum', 'Detector Integration', None,
     [r'BeamPipe(B0)?', r'B0Window', r'Pipe_.*', r'.*_BeamlineMagnet', r'Magnets_Q3eR',
      r'VacuumMagnetElement']),
]
# Topics production reads that belong to no single detector.
TOPICS = ['Backgrounds and Rates', 'Radiation Level', 'Detector Integration',
          'Readout Electronics and Data Acquisition', 'Software and Computing',
          'Incorporation of Beam Backgrounds in the Studies', 'Polarimeters']

INC = re.compile(r'\\(subfile|input|include)\{([^}]+)\}')
SEC = re.compile(r'\\(chapter|section|subsection|subsubsection|paragraph)\*?\{')
LEVEL = {'chapter': 0, 'section': 1, 'subsection': 2, 'subsubsection': 3, 'paragraph': 4}


def braced(text, start):
    """The contents of the brace group opening at text[start - 1]."""
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {'{': 1, '}': -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def plain(title):
    """A LaTeX title as the PDF outline shows it, then normalised for matching."""
    m = re.search(r'\\texorpdfstring\{', title)
    if m:
        first = braced(title, m.end())
        rest = title[m.end() + len(first) + 1:]
        if rest.startswith('{'):
            title = title[:m.start()] + braced(rest, 1) + rest[len(braced(rest, 1)) + 2:]
    title = re.sub(r'\\[a-zA-Z]+\*?', ' ', title)
    return title


def norm(title):
    return re.sub(r'[^a-z0-9]+', '', plain(title).lower())


def latex_walk(root):
    """Headings of the report's LaTeX in document order, each with its file and
    line, and for every walked file the heading active at each line change."""
    headings, files, active = [], [], {}
    seen = set()

    def resolve(base, ref):
        for cand in (os.path.join(base, ref), os.path.join(root, ref)):
            for c in (cand, cand + '.tex'):
                if os.path.isfile(c):
                    return c
        return None

    def walk(path, depth=0):
        if path in seen or depth > 12:
            return
        seen.add(path)
        rel = os.path.relpath(path, root)
        files.append(rel)
        marks = active.setdefault(rel, [])
        marks.append([1, len(headings) - 1])
        for no, line in enumerate(open(path, errors='replace'), 1):
            code = line.split('%', 1)[0] if not line.lstrip().startswith('\\%') else line
            m = INC.search(code)
            if m:
                sub = resolve(os.path.dirname(path), m.group(2).strip())
                if sub:
                    walk(sub, depth + 1)
                    marks.append([no + 1, len(headings) - 1])
                continue
            s = SEC.search(code)
            if s:
                title = braced(code, s.end())
                label = re.search(r'\\label\{([^}]+)\}', code)
                headings.append({'level': LEVEL[s.group(1)], 'title': plain(title).strip(),
                                 'file': rel, 'line': no,
                                 'label': label.group(1) if label else ''})
                marks.append([no, len(headings) - 1])
    walk(os.path.join(root, 'PDR.tex'))
    return headings, files, active


def outline(pdf_path):
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    labels = reader.page_labels
    rows = []

    def walk(items, depth=0):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
            else:
                try:
                    page = reader.get_destination_page_number(it)
                except Exception:
                    page = None
                rows.append({'depth': depth, 'title': ' '.join(it.title.split()),
                             'pdf_page': None if page is None else page + 1})
    walk(reader.outline)
    for i, row in enumerate(rows):
        nxt = next((r['pdf_page'] for r in rows[i + 1:]
                    if r['depth'] <= row['depth'] and r['pdf_page']), len(reader.pages))
        row['pdf_end'] = max(row['pdf_page'] or 0, nxt) if row['pdf_page'] else None
        row['page'] = labels[row['pdf_page'] - 1] if row['pdf_page'] else ''
        row['page_end'] = labels[row['pdf_end'] - 1] if row['pdf_end'] else ''
    return rows, len(reader.pages)


def attach_files(rows, headings, active):
    """Give each outline entry the LaTeX heading it is (document order, matched
    by normalised title) and the files its text lives in."""
    j = 0
    for row in rows:
        want = norm(row['title'])
        for k in range(j, min(j + 40, len(headings))):
            if norm(headings[k]['title']) == want:
                row['tex'] = f"{headings[k]['file']}:{headings[k]['line']}"
                row['heading'] = k
                j = k + 1
                break
    # files: those whose active-heading marks fall inside this heading's span
    owner = {}
    for rel, marks in active.items():
        for _, h in marks:
            if h >= 0:
                owner.setdefault(h, set()).add(rel)
    for idx, row in enumerate(rows):
        h = row.get('heading')
        if h is None:
            continue
        end = next((r['heading'] for r in rows[idx + 1:]
                    if r.get('heading') is not None and r['depth'] <= row['depth']),
                   len(headings))
        row['files'] = sorted(set().union(*[owner.get(k, set()) for k in range(h, end)]))


def geometry(directory):
    seen, dets = set(), []

    def walk(path):
        if path in seen or not os.path.isfile(path):
            return
        seen.add(path)
        text = re.sub(r'<!--.*?-->', '', open(path, errors='replace').read(), flags=re.S)
        for m in re.finditer(r'<detector\b([^>]*)>', text):
            name = re.search(r'\bname="([^"]+)"', m.group(1))
            kind = re.search(r'\btype="([^"]+)"', m.group(1))
            if name:
                dets.append({'name': name.group(1), 'type': kind.group(1) if kind else '',
                             'compact': os.path.relpath(path, directory)})
        for ref in re.findall(r'<include\s+ref="([^"]+)"', text):
            ref = ref.replace('${DETECTOR_PATH}', directory)
            walk(os.path.normpath(ref if os.path.isabs(ref)
                                  else os.path.join(os.path.dirname(path), ref)))
    walk(os.path.join(directory, CONFIG))
    return dets


def find(rows, title, after_depth0=None):
    want = norm(title)
    for row in rows:
        if norm(row['title']) == want:
            return row
    return None


def build(pdr, geometry_dir):
    rows, pages = outline(os.path.join(pdr, PDF_NAME))
    headings, files, active = latex_walk(os.path.join(pdr, 'source'))
    attach_files(rows, headings, active)
    dets = geometry(geometry_dir)

    def entry(row):
        if not row:
            return None
        kids = []
        start = rows.index(row)
        for r in rows[start + 1:]:
            if r['depth'] <= row['depth']:
                break
            if r['depth'] == row['depth'] + 1:
                kids.append({'title': r['title'], 'page': r['page'], 'pdf_page': r['pdf_page'],
                             'tex': r.get('tex', '')})
        return {'title': row['title'], 'page': row['page'], 'page_end': row['page_end'],
                'pdf_page': row['pdf_page'], 'pdf_end': row['pdf_end'],
                'tex': row.get('tex', ''), 'files': row.get('files', []), 'parts': kids}

    systems, assigned = [], {}
    for name, heading, req, patterns in SYSTEMS:
        members = [d['name'] for d in dets
                   if any(re.fullmatch(p, d['name']) for p in patterns)]
        for m in members:
            assigned[m] = name
        systems.append({'system': name, 'section': entry(find(rows, heading)),
                        'requirements': entry(find(rows, req)) if req else None,
                        'detectors': members})
    unassigned = [d['name'] for d in dets if d['name'] not in assigned]
    topics = [{'topic': t, 'section': entry(find(rows, t))} for t in TOPICS]
    # line marks for the grep tool: file -> [[line, heading index]]
    marks = {rel: m for rel, m in active.items()}
    for h in headings:
        h.pop('label', None) if not h.get('label') else None
    return {
        'built': date.today().isoformat(),
        'report': {'title': 'ePIC Preliminary Design Report', 'version': '3.1',
                   'doi': '10.5281/zenodo.18271602', 'license': 'CC BY 4.0',
                   'pdf': os.path.join(pdr, PDF_NAME), 'source': os.path.join(pdr, 'source'),
                   'pages': pages},
        'geometry': {'release': GEOMETRY_RELEASE, 'config': CONFIG, 'path': geometry_dir,
                     'detectors': len(dets)},
        'systems': systems, 'topics': topics,
        'detectors': {d['name']: {'system': assigned.get(d['name'], ''), 'type': d['type'],
                                  'compact': d['compact']} for d in dets},
        'unassigned': unassigned,
        'outline': rows, 'headings': headings, 'marks': marks,
    }


def markdown(index):
    out = ['| System | DD4hep detectors | PDR section | Pages | Requirements |',
           '|---|---|---|---|---|']
    for s in index['systems']:
        sec, req = s['section'] or {}, s['requirements'] or {}
        names = ', '.join(f'`{n}`' for n in s['detectors']) or '-'
        pages = f"{sec.get('page', '')}–{sec.get('page_end', '')}" if sec else '-'
        reqs = f"{req['title']}, p. {req['page']}" if req else '-'
        out.append(f"| {s['system']} | {names} | {sec.get('title', '-')} | {pages} | {reqs} |")
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--pdr', default=PDR_DIR)
    ap.add_argument('--geometry', default='')
    ap.add_argument('--out', default=OUT)
    ap.add_argument('--markdown', action='store_true')
    args = ap.parse_args()
    geo = args.geometry
    if not geo:
        link = os.readlink(os.path.join(IMAGE, f'opt/detector/epic-{GEOMETRY_RELEASE}/share/epic'))
        geo = IMAGE + link
    index = build(args.pdr, geo)
    if args.markdown:
        print(markdown(index))
        return
    with open(args.out, 'w') as fh:
        json.dump(index, fh, indent=1, ensure_ascii=False)
        fh.write('\n')
    print(f"wrote {args.out}: {len(index['systems'])} systems, "
          f"{len(index['detectors'])} detectors ({len(index['unassigned'])} unassigned), "
          f"{len(index['outline'])} outline entries, "
          f"{sum(1 for r in index['outline'] if r.get('tex'))} matched to LaTeX")
    if index['unassigned']:
        print('unassigned:', ', '.join(index['unassigned']), file=sys.stderr)
    missing = [s['system'] for s in index['systems'] if not s['section']]
    if missing:
        print('no PDR section found for:', ', '.join(missing), file=sys.stderr)


if __name__ == '__main__':
    main()
