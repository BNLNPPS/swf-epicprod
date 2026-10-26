#!/usr/bin/env python3
"""Extract complete EVGEN events into a checked, range-local HepMC3 file.

Uses HepMC3 directly, independently of DD4hep or Geant4. Event positions are
zero based; the original HepMC event numbers and complete records are kept.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time


def hepmc():
    import ROOT
    if not getattr(hepmc, 'loaded', False):
        if ROOT.gSystem.Load('libHepMC3rootIO') < 0:
            raise RuntimeError('HepMC3 ROOT input library is unavailable')
        if not ROOT.gInterpreter.Declare('''
            #include <HepMC3/ReaderRootTree.h>
            #include <HepMC3/ReaderAscii.h>
            #include <HepMC3/WriterAscii.h>
            #include <sstream>
            namespace EpicprodEvgen {
                std::string canonical(const HepMC3::GenEvent& event) {
                    std::ostringstream text;
                    HepMC3::WriterAscii writer(text, event.run_info());
                    writer.set_precision(17);
                    writer.write_event(event);
                    writer.close();
                    return text.str();
                }
            }
        '''):
            raise RuntimeError('Cannot load HepMC3 range extraction')
        hepmc.loaded = True
    return ROOT


def reader_for(source):
    root = hepmc()
    kind = root.HepMC3.ReaderRootTree if str(source).endswith('.root') else root.HepMC3.ReaderAscii
    reader = kind(str(source))
    if reader.failed():
        raise RuntimeError('Cannot open EVGEN input: ' + str(source))
    return reader


def event_identity(event):
    """Hash all serialized event/run content, including attributes and weights."""
    root = hepmc()
    return hashlib.sha256(str(root.EpicprodEvgen.canonical(event)).encode()).hexdigest()


def inspect_events(path):
    root = hepmc()
    reader = reader_for(path)
    events = []
    try:
        while True:
            event = root.HepMC3.GenEvent()
            if not reader.read_event(event) or reader.failed():
                break
            events.append({'event_number': int(event.event_number()),
                           'particles': len(event.particles()),
                           'vertices': len(event.vertices()),
                           'sha256': event_identity(event)})
    finally:
        reader.close()
    return events


def prepare(source, first, count, output, source_id=None):
    if type(first) is not int or first < 0 or type(count) is not int or count <= 0:
        raise ValueError('EVGEN position must be nonnegative and count positive')
    output = Path(output).resolve()
    if output.exists() or Path(str(output) + '.json').exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + f'.{os.getpid()}.tmp')
    if temporary.exists():
        raise FileExistsError(temporary)
    started = time.time()
    root = hepmc()
    reader = reader_for(source)
    writer = root.HepMC3.WriterAscii(str(temporary))
    writer.set_precision(17)
    selected = []
    try:
        if first and not reader.skip(first):
            raise RuntimeError(f'Cannot position EVGEN input at event {first}')
        for index in range(count):
            event = root.HepMC3.GenEvent()
            if not reader.read_event(event) or reader.failed():
                raise RuntimeError(f'EVGEN input ended at position {first + index}; expected {count} events')
            selected.append({'event_number': int(event.event_number()),
                             'particles': len(event.particles()),
                             'vertices': len(event.vertices()),
                             'sha256': event_identity(event)})
            writer.write_event(event)
            if writer.failed():
                raise RuntimeError('Writing EVGEN range failed')
    finally:
        reader.close()
        writer.close()
    if inspect_events(temporary) != selected:
        raise RuntimeError('EVGEN range serialization changed event content')
    temporary.replace(output)
    result = {'schema': 'epicprod-evgen-range/1', 'source': str(source),
              'source_id': source_id or str(source), 'first_event': first,
              'events': count, 'file': str(output), 'selected': selected,
              'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
              'started_at': started, 'ended_at': time.time()}
    manifest = Path(str(output) + '.json')
    temp_manifest = Path(str(manifest) + '.tmp')
    temp_manifest.write_text(json.dumps(result, indent=2) + '\n')
    temp_manifest.replace(manifest)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--first-event', type=int, required=True)
    parser.add_argument('--events', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-id')
    args = parser.parse_args()
    result = prepare(args.input, args.first_event, args.events, args.output, args.source_id)
    print(json.dumps({k: v for k, v in result.items() if k != 'selected'}), flush=True)


if __name__ == '__main__':
    main()
