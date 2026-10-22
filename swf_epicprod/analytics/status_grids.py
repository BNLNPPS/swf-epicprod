"""Production status grids: per generator family, beam energy against Q²
range, each cell a sample's state in one campaign (docs/CAMPAIGN_DELIVERY.md,
Surfaces, the status grids).

A cell is an EVGEN sample. Its state joins three records the system already
keeps, so the build makes no Rucio or PanDA call:

- registration: the recorded JLab Rucio EVGEN inventory (the evgen-rucio
  snapshot the EVGEN update writes);
- what the campaign asks for and made: the campaign's physics
  configurations, reached from an EVGEN path through the EVGEN-stage records
  bound to them and through the produced outputs (the EVGEN convention map);
- delivery: the campaign-completion cached product, the campaign plan's own
  delivered events and targets per configuration.

States: ``complete`` (the plan's complete status), ``partial`` (events
delivered, below target or without one), ``registered`` (registered, nothing
delivered in the campaign), ``missing`` (a configuration in the campaign with
nothing delivered and its EVGEN sample not registered). Registration is a
second fact on every cell: a sample delivered from an input never registered
is complete or partial and says so. A combination of a family's beams and Q²
ranges that no sample or configuration names is left empty.
"""
import logging
import re

logger = logging.getLogger(__name__)

PRODUCT_KEY = 'status_grids:v1:{campaign}'
PRODUCT_TTL_SECONDS = 3600

# A beam token: electron x hadron energy, e.g. 10x100, 18x137.5.
BEAM_RE = re.compile(r'^(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$')
# A Q² token: q2_1to10, q2_1000toINF, q2_10_20, q2_0to0.01, minQ2=10.
Q2_RANGE_RE = re.compile(r'^q2_([\d.]+)(?:to|_)([\d.]+|INF)$', re.IGNORECASE)
Q2_FLOOR_RE = re.compile(r'^(?:minQ2=|q2_)([\d.]+)$', re.IGNORECASE)
# A retry segment of a produced output's path (/RECO/<v>/<config>/try2/...),
# which names the same sample as the base path.
TRY_RE = re.compile(r'^try\d+$')
# Areas left out: background samples are mixing inputs, not production signal.
SKIPPED_AREAS = ('BACKGROUNDS',)
DOOR_PREFIX = '/volatile/eic/EPIC'


def evgen_path(location):
    """``/EVGEN/<tail>`` from a DID (``epic:/EVGEN/...``), a door path
    (``/volatile/eic/EPIC/EVGEN/...``) or a tail; '' when none."""
    text = str(location or '').strip()
    if ':' in text and not text.startswith('/'):
        text = text.partition(':')[2]
    if text.startswith(DOOR_PREFIX):
        text = text[len(DOOR_PREFIX):]
    text = '/' + text.lstrip('/')
    if not text.startswith('/EVGEN/'):
        text = '/EVGEN' + text
    tokens = [t for t in text.split('/') if t][1:]
    while tokens and TRY_RE.match(tokens[0]):
        tokens = tokens[1:]
    return '/EVGEN/' + '/'.join(tokens) if tokens else ''


def q2_label(token):
    """How a Q² token reads in the grid's header, in GeV²."""
    if not token:
        return 'all'
    hit = Q2_RANGE_RE.match(token)
    if hit:
        low, high = hit.groups()
        return f'> {low}' if high.upper() == 'INF' else f'{low} to {high}'
    hit = Q2_FLOOR_RE.match(token)
    return f'≥ {hit.group(1)}' if hit else token


def _q2_order(token):
    hit = Q2_RANGE_RE.match(token or '')
    if hit:
        low, high = hit.groups()
        return (float(low), float('inf') if high.upper() == 'INF' else float(high))
    hit = Q2_FLOOR_RE.match(token or '')
    if hit:
        return (float(hit.group(1)), float('inf'))
    return (float('inf'), float('inf'))


def _beam_order(token):
    hit = BEAM_RE.match(token or '')
    return (float(hit.group(1)), float(hit.group(2))) if hit else (float('inf'), 0.0)


def split_path(path):
    """(area, family, beam, q2, sample) of an EVGEN path, or None for a path
    outside the grids. A produced output mixed with backgrounds carries the
    background chain ahead of the physics area (``Bkg_*/GoldCt/10um/DIS/...``):
    the chain names the family's background and ``sample`` is the EVGEN path
    of the signal, the one registration is read for. The family is the path
    without its beam and Q² segments."""
    from pcs.physics_match import _KNOWN_AREAS
    tokens = [t for t in path.split('/') if t][1:]  # drop 'EVGEN'
    start = next((i for i, t in enumerate(tokens) if t in _KNOWN_AREAS), None)
    if start is None or tokens[start] in SKIPPED_AREAS:
        return None
    background, tokens = tokens[:start], tokens[start:]
    beam = next((t for t in tokens if BEAM_RE.match(t)), '')
    q2 = next((t for t in tokens if Q2_RANGE_RE.match(t) or Q2_FLOOR_RE.match(t)), '')
    family = '/'.join(t for t in tokens if t not in (beam, q2))
    if background:
        family += ', with background ' + '/'.join(background)
    return tokens[0], family, beam, q2, '/EVGEN/' + '/'.join(tokens)


def events_label(n, unit=None):
    """Events as a person reads them, 4.95M or 66.3k; ``unit`` ('M' or
    'k') holds a delivered/target pair to one unit."""
    if n is None:
        return ''
    n = float(n)
    unit = unit or ('M' if n >= 1e6 else 'k' if n >= 1e3 else '')
    if unit == 'M':
        return f'{n / 1e6:.2f}M'
    if unit == 'k':
        return f'{n / 1e3:.1f}k'
    return f'{int(n)}'


def pair_label(delivered, target):
    """'4.95M / 5.00M', in the unit of the larger; 'no target' without one."""
    top = max(float(delivered or 0), float(target or 0))
    # 999,999 reads as 1.00M beside its neighbours' 1.00M, not as 1000.0k
    unit = 'M' if top >= 0.9995e6 else 'k' if top >= 1e3 else ''
    shown = events_label(delivered or 0, unit)
    return f'{shown} / {events_label(target, unit)}' if target else f'{shown}, no target'


def build_status_grids(campaign_name):
    """The grids of one campaign. Raises when the EVGEN inventory or the
    completion record cannot be read, so a missing input never stores as
    an empty product."""
    from monitor_app.cached_product import get_product
    from pcs.models import Campaign, Dataset, ProdTask
    from pcs.services import evgen_convention_paths_cached, evgen_registered_paths
    from swf_epicprod.analytics.completion import completion_product_value

    campaign = Campaign.objects.filter(name=campaign_name).first()
    if campaign is None:
        raise ValueError(f'no campaign {campaign_name!r}')
    registered = evgen_registered_paths()
    if not registered:
        raise RuntimeError('the EVGEN Rucio inventory is not recorded')
    product = get_product('prod_hub_campaign_completion', completion_product_value,
                          ttl_seconds=600)
    entry = next((e for e in (product['value'] or [])
                  if e.get('campaign') == campaign_name), None)
    completion = {r['pc']: r for r in ((entry or {}).get('pcs') or [])}

    # The campaign's configurations and an edition of each, for the link.
    edition_by_pc = {}
    for label, name in (Dataset.objects
                        .filter(campaign=campaign, physics_config__isnull=False)
                        .order_by('pk')
                        .values_list('physics_config__label', 'composed_name')):
        edition_by_pc.setdefault(label, name)

    # The configurations behind each EVGEN path, three ways: an EVGEN-stage
    # record that matched the registered dataset; the produced output (the
    # payload's path law, /RECO/<campaign>/<config>/<tail> is made from
    # /EVGEN/<tail>: the EVGEN convention map), which reaches samples
    # produced from inputs never registered; and an EVGEN-stage record whose
    # request names a path Rucio does not have.
    pcs_by_path = {}
    for record in (Dataset.objects.filter(metadata__stage='evgen',
                                          physics_config__isnull=False)
                   .select_related('physics_config')):
        label = record.physics_config.label
        metadata = record.metadata or {}
        dids = [m.get('did') for m in ((metadata.get('rucio') or {}).get('matched') or [])
                if m.get('did')]
        for did in dids:
            pcs_by_path.setdefault(evgen_path(did), set()).add(label)
        if not dids:
            path = evgen_path((metadata.get('source') or {}).get('location')
                              or getattr(record, 'source_location', ''))
            if path:
                pcs_by_path.setdefault(path, set()).add(label)
    task_pc = {t.composed_name: t.dataset.physics_config.label
               for t in (ProdTask.objects
                         .filter(campaign=campaign, dataset__physics_config__isnull=False)
                         .select_related('dataset__physics_config'))}
    convention = evgen_convention_paths_cached() or {}
    for path, info in convention.items():
        if path.startswith('/EVGEN/'):
            for task in info.get('tasks') or []:
                if task in task_pc:
                    pcs_by_path.setdefault(evgen_path(path), set()).add(task_pc[task])

    cells = {}
    for path in set(registered) | set(pcs_by_path):
        parts = split_path(path)
        if parts is None:
            continue
        # The campaign's configuration for the sample: the one with the
        # most delivered, when several name it.
        in_campaign = [pc for pc in (pcs_by_path.get(path) or ()) if pc in edition_by_pc]
        pc = max(in_campaign, key=lambda p: ((completion.get(p) or {}).get('delivered_events') or 0,
                                             p), default=None)
        is_registered = parts[4] in registered
        if pc is None and path not in registered:
            continue  # neither registered nor asked for by this campaign
        row = completion.get(pc) or {}
        if row.get('status') == 'complete':
            state = 'complete'
        elif row.get('delivered_events'):
            state = 'partial'
        elif is_registered:
            state = 'registered'
        else:
            state = 'missing'
        cells[path] = {
            'path': path, 'area': parts[0], 'family': parts[1],
            'beam': parts[2], 'q2': parts[3], 'state': state, 'pc': pc,
            'registered': is_registered,
            'edition': edition_by_pc.get(pc) if pc else None,
            'delivered': row.get('delivered_events'),
            'target': row.get('target'),
            'label': (pair_label(row.get('delivered_events'), row.get('target'))
                      if state in ('complete', 'partial') else
                      'reg.' if state == 'registered' else 'missing'),
            'in_flight': bool(row.get('in_flight')),
        }
    placed = {c['pc'] for c in cells.values() if c['pc']}
    no_evgen_path = len(set(edition_by_pc) - placed)

    # Families, each a grid over the beams and Q² ranges its samples name.
    families = {}
    for cell in cells.values():
        families.setdefault((cell['area'], cell['family']), []).append(cell)
    grids = []
    for (area, family), members in sorted(families.items()):
        beams = sorted({c['beam'] for c in members}, key=_beam_order)
        q2s = sorted({c['q2'] for c in members}, key=_q2_order)
        by_key = {(c['beam'], c['q2']): c for c in members}
        rows = [{'beam': b or 'all',
                 'cells': [by_key.get((b, q)) for q in q2s]} for b in beams]
        grids.append({'area': area, 'family': family,
                      'columns': [{'token': q, 'label': q2_label(q)} for q in q2s],
                      'rows': rows,
                      'counts': _counts(members)})
    return {
        'campaign': campaign_name,
        'grids': grids,
        'counts': _counts(list(cells.values())),
        # campaign configurations no EVGEN path reaches, so in no grid
        'configurations': len(edition_by_pc),
        'no_evgen_path': no_evgen_path,
        'completion_built_at': (product['built_at'].isoformat()
                                if product.get('built_at') else None),
    }


def _counts(cells):
    out = {'complete': 0, 'partial': 0, 'registered': 0, 'missing': 0}
    for c in cells:
        out[c['state']] += 1
    out['samples'] = len(cells)
    return out


def status_grids_product(campaign_name, refresh=False):
    """The stored grids of a campaign (CACHED_PRODUCTS.md): served at once,
    rebuilt behind the page when older than the TTL. Their inputs refresh
    nightly (the EVGEN sweep, the delivery rebuild), so an hourly rebuild
    keeps the grids as current as their sources."""
    from monitor_app.cached_product import get_product
    return get_product(PRODUCT_KEY.format(campaign=campaign_name),
                       lambda: build_status_grids(campaign_name),
                       ttl_seconds=PRODUCT_TTL_SECONDS, refresh=refresh)
