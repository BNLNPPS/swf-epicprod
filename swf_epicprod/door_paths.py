"""Where the EVGEN tree sits on the JLab doors, and the door path form a
catalog record or an operator may give for it.

A sample's identity is its Rucio name, ``epic:/EVGEN/<tail>``. A door path
is that name under EIC-XRD's protocol prefix. The values here are the
RSE's own, as Rucio lists them: ``root://dtn-rucio.jlab.org:1094`` with
prefix ``/cache/eic/EPIC`` (read and write), and the read-only
``root://dtn2304.jlab.org:8443`` with prefix
``/jlab-osdf-ro/eic/EPIC/cache`` (docs/EPICPROD_EVGEN_INPUTS.md).
"""

from __future__ import annotations

# EIC-XRD's prefix on the Rucio door (root://dtn-rucio.jlab.org:1094).
DOOR_PREFIX = '/cache/eic/EPIC'

# The anonymous read door: JLab's read-only OSDF origin, which the payload
# streams EVGEN input from (payload run.sh, XRDRURL and XRDRBASE). It
# serves the same area under its own path.
OSDF_DOOR = 'root://dtn2304.jlab.org:8443'
OSDF_PATH = '/jlab-osdf-ro/eic/EPIC/cache'
OSDF_BASE = f'{OSDF_DOOR}/{OSDF_PATH}'


def split_door_prefix(path, prefixes=(DOOR_PREFIX,)):
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
