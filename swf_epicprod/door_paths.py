"""Where the EVGEN tree sits on the JLab doors, and the door path forms a
catalog record or an operator may give for it.

A sample's identity is its Rucio name, ``epic:/EVGEN/<tail>``. A door path
is that name under a prefix: ``/cache/eic/EPIC``, EIC-XRD's protocol
prefix, or ``/volatile/eic/EPIC``, its former prefix, which stored PCS
records and the production team's ``default_datasets`` catalogue still
carry. Both name the same files, so every reader takes the tail and reads
either prefix alike (docs/EPICPROD_EVGEN_INPUTS.md).
"""

from __future__ import annotations

# EIC-XRD's prefix on the Rucio door (root://dtn-rucio.jlab.org:1094),
# current first.
DOOR_PREFIXES = ('/cache/eic/EPIC', '/volatile/eic/EPIC')

# The anonymous read door: JLab's read-only OSDF origin, which the payload
# streams EVGEN input from (payload run.sh, XRDRURL and XRDRBASE). It
# serves the same area under its own path, current first.
OSDF_DOOR = 'root://dtn2304.jlab.org:8443'
OSDF_PATHS = ('/jlab-osdf-ro/eic/EPIC/cache', '/jlab-osdf-ro/eic/EPIC/volatile')
OSDF_PATH = OSDF_PATHS[0]
OSDF_BASE = f'{OSDF_DOOR}/{OSDF_PATH}'


def split_door_prefix(path, prefixes=DOOR_PREFIXES):
    """``(prefix, rest)`` for a path under one of ``prefixes``, given with
    or without its leading slash; ``('', path)`` for any other."""
    text = str(path or '')
    rooted = '/' + text.lstrip('/')
    for prefix in prefixes:
        if rooted == prefix or rooted.startswith(prefix + '/'):
            return prefix, rooted[len(prefix):]
    return '', text


def strip_door_prefix(path):
    """``path`` less its door prefix and the slash after it; unchanged
    when it carries none."""
    prefix, rest = split_door_prefix(path)
    return rest.lstrip('/') if prefix else rest


def evgen_tail(path):
    """``<tail>`` of a door path ``<door prefix>/EVGEN/<tail>``, case
    preserved ('' when nothing follows ``EVGEN``); None for any other
    path."""
    prefix, rest = split_door_prefix(path)
    head, _, tail = rest.strip('/').partition('/')
    return tail if prefix and head == 'EVGEN' else None


def door_forms(path):
    """An absolute door path written under each door prefix, current
    first; ``(path,)`` when it carries no door prefix. A record is found
    by whichever form it was stored under."""
    text = str(path or '')
    prefix, rest = split_door_prefix(text) if text.startswith('/') else ('', text)
    return tuple(p + rest for p in DOOR_PREFIXES) if prefix else (text,)
