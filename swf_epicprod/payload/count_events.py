#!/usr/bin/env python3
"""Print the event count of a podio ROOT file: the entry count of its
``events`` tree, the count the epicprod payload reports and registers
(swf-epicprod docs/RUCIO_REGISTRATION_CONTRACT.md). ``--tree`` names
another tree, ``hepmc3_tree`` for the frames of a merged HepMC3 file.
Exits 1 with the reason on stderr when the file or the tree cannot be
read, so a caller never mistakes silence for zero.

Usage:
  count_events.py [--tree NAME] <file.root>
"""

import sys

EVENTS_TREE = "events"


def main():
    args = sys.argv[1:]
    tree_name = EVENTS_TREE
    if len(args) == 3 and args[0] == "--tree":
        tree_name, args = args[1], args[2:]
    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} [--tree NAME] <file.root>", file=sys.stderr)
        return 2
    path = args[0]
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
    tree = tfile.Get(tree_name)
    if not tree:
        print(f"count_events: no {tree_name} tree in {path}", file=sys.stderr)
        tfile.Close()
        return 1
    print(int(tree.GetEntries()))
    tfile.Close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
