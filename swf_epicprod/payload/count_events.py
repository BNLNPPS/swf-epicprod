#!/usr/bin/env python3
"""Print the event count of a podio ROOT file: the entry count of its
``events`` tree, the count the epicprod payload reports and registers
(swf-epicprod docs/RUCIO_REGISTRATION_CONTRACT.md). Exits 1 with the
reason on stderr when the file or the tree cannot be read, so a caller
never mistakes silence for zero.

Usage:
  count_events.py <podio.root>
"""

import sys

EVENTS_TREE = "events"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <podio.root>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    try:
        import ROOT
    except ImportError:
        print("count_events: PyROOT is not available", file=sys.stderr)
        return 1
    ROOT.gErrorIgnoreLevel = ROOT.kError
    tfile = ROOT.TFile.Open(path)
    if not tfile or tfile.IsZombie():
        print(f"count_events: cannot open {path}", file=sys.stderr)
        return 1
    tree = tfile.Get(EVENTS_TREE)
    if not tree:
        print(f"count_events: no {EVENTS_TREE} tree in {path}", file=sys.stderr)
        tfile.Close()
        return 1
    print(int(tree.GetEntries()))
    tfile.Close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
