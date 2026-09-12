"""What the OSG submission excludes, and the evidence for each entry.

The operative control is the submit description both production OSG
queues share on the submit host, `submit_pilot2_push_bnl_osg.sdf`:
`+UNDESIRED_Sites` excludes a site and a `Requirements` clause excludes
a worker node named together with its site (docs/OSG_SUBMISSION.md,
Excluding what delivers nothing). That file cannot be read on a
schedule from the monitor host — ssh to the submit host is refused
except through the facility gateway with agent forwarding — so this
module is the rendered source, and `scripts/check-osg-exclusions.py`
compares it against the deployed file so a disagreement is reported
rather than silently displayed.

A node is identified by site and node together, never by node alone:
the pool advertises bare names such as `compute05`, `n358` and
`fc20621` alongside fully qualified ones, and a name on its own would
ban a healthy machine at another site with nothing to show it had.

Evidence is the job record over the thirty days ending on `AS_OF`:
every entry finished nothing at all in that window.
"""

AS_OF = '2026-09-07'
APPLIED = '2026-09-07'
SUBMIT_FILE = '/var/data/atlpan/harvester_common/submit_pilot2_push_bnl_osg.sdf'
QUEUES = ('BNL_OSG_EPIC_PROD_1', 'BNL_OSG_PanDA_1')

# Sites excluded whole, by +UNDESIRED_Sites. The ePIC entries carry the
# measurement that put them there; the rest predate this record and are
# kept as they stand.
EXCLUDED_SITES = [
    {'site': 'Alabama-CHPC', 'failed': 13218, 'finished': 0,
     'core_hours': 8052, 'nodes': 19, 'applied': '2026-09-07',
     'reason': 'every node in the record finished nothing; where trial '
               '39305 lost 100 reconstructed events to three timeouts '
               'against the JLab catalog it cannot reach'},
    {'site': 'UCSD', 'reason': 'excluded before this record was kept'},
    {'site': 'FNAL', 'reason': 'excluded before this record was kept'},
    {'site': 'MI-HORUS', 'reason': 'excluded before this record was kept'},
    {'site': 'GREX', 'reason': 'excluded from the OSG pool before this '
                               'record was kept; the dedicated GREX queue '
                               'submits to its own compute element'},
    {'site': 'BNL-SDCC', 'reason': 'excluded before this record was kept'},
]

# Nodes excluded individually, by the Requirements clause. Each sits at a
# site that otherwise delivers, which is what makes node granularity the
# right instrument rather than excluding the site.
EXCLUDED_SITE_NODES = [
    {'site': 'Nebraska', 'node': 'red-c0823.unl.edu',
     'failed': 121, 'finished': 0, 'core_hours': 367},
    {'site': 'Nebraska', 'node': 'red-c7228.unl.edu',
     'failed': 82, 'finished': 0, 'core_hours': 204},
    {'site': 'Nebraska', 'node': 'red-c0831.unl.edu',
     'failed': 86, 'finished': 0, 'core_hours': 187},
    {'site': 'UConn', 'node': 'nod58.phys.uconn.edu',
     'failed': 257, 'finished': 0, 'core_hours': 284},
    {'site': 'Rhodes-HPC', 'node': 'compute05',
     'failed': 119, 'finished': 0, 'core_hours': 217},
    {'site': 'GREX', 'node': 'n358',
     'failed': 145, 'finished': 0, 'core_hours': 215},
    {'site': 'ComputeCanada-Fir', 'node': 'fc30438',
     'failed': 128, 'finished': 0, 'core_hours': 142},
    {'site': 'ComputeCanada-Fir', 'node': 'fc20637',
     'failed': 143, 'finished': 0, 'core_hours': 133},
    {'site': 'ComputeCanada-Fir', 'node': 'fc20621',
     'failed': 132, 'finished': 0, 'core_hours': 130},
    {'site': 'ComputeCanada-Fir', 'node': 'fc20667',
     'failed': 102, 'finished': 0, 'core_hours': 128},
    {'site': 'ComputeCanada-Fir', 'node': 'fc20624',
     'failed': 119, 'finished': 0, 'core_hours': 111},
    {'site': 'ComputeCanada-Fir', 'node': 'fc30416',
     'failed': 96, 'finished': 0, 'core_hours': 102},
    # Added 2026-09-12 from the segfault catalog (finding exit139:task38914):
    # 187 of task 38914's 252 crashes were this one host, every one dead at
    # 35.5 minutes; the same thirty-day window as the rest, and over ninety
    # days 480 failed against 4 finished, none since 2026-07-21.
    {'site': 'BEOCAT-SLATE', 'node': 'warlock12',
     'failed': 271, 'finished': 0, 'core_hours': 161, 'applied': '2026-09-12'},
]


def excluded_nodes_by_site():
    """The node exclusions grouped by site, each site's nodes in order."""
    grouped = {}
    for entry in EXCLUDED_SITE_NODES:
        grouped.setdefault(entry['site'], []).append(entry)
    return grouped


def requirements_clause():
    """The Requirements expression this list implies, for the drift check.

    Built from the same data the page renders, so the check compares the
    deployed file against what is displayed rather than against a second
    hand-written copy.
    """
    terms = []
    for site, entries in excluded_nodes_by_site().items():
        names = '|'.join(e['node'].split('.')[0] for e in entries)
        terms.append(f'(GLIDEIN_Site =?= "{site}" && '
                     f'regexp("^({names})([.]|$)", Machine) =?= True)')
    return '!( ' + ' || '.join(terms) + ' )'


def totals():
    """What the exclusions together were costing, from the same window."""
    nodes = EXCLUDED_SITE_NODES
    sites = [s for s in EXCLUDED_SITES if s.get('failed')]
    return {
        'node_count': len(nodes),
        'site_count': len(sites),
        'failed': sum(e['failed'] for e in nodes) + sum(s['failed'] for s in sites),
        'core_hours': (sum(e['core_hours'] for e in nodes)
                       + sum(s['core_hours'] for s in sites)),
    }
