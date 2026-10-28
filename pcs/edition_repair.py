"""Bounded rebinding of named editions after a derivation correction
(pythia6-lhapdf 1.1.0 x bins, 2026-10-27).

A corrected derivation changes the configuration key an edition derives,
but an issued PhysicsConfig cannot be re-keyed in place (the database
identity guard). The repair resaves exactly the editions bound to the
named configurations, so Dataset.save -> ensure_physics_config binds each
to the configuration its corrected identity names, and then supersedes
each old configuration with the one its editions all moved to. Names,
source DIDs, targets and request/task links stay on the edition rows,
which are not otherwise changed.

``plan_rebind`` reads only; ``classify`` and ``plan_problems`` are pure;
``apply_rebind`` plans again and writes, in one transaction, only a plan
in which every configuration moves whole.

The database runs READ COMMITTED, so the transaction alone does not hold
what the plan checked. ``apply_rebind`` first locks the named
configurations and their editions (``lock_scope``), in primary-key
order, and only then plans. The configuration lock blocks a concurrent
edition from binding to an old configuration (an insert or rebind takes
FOR KEY SHARE on the referenced row) and blocks concurrent requestor,
metadata or lifecycle changes until commit. Existing targets are locked
and checked again after planning, and no edition may remain on an old
configuration when it is superseded.
"""

#: Per old configuration, what a resave would do to its editions.
MOVES = 'moves'          # every edition to one other key: supersede
UNCHANGED = 'unchanged'  # every edition keeps the key: nothing to do
SPLIT = 'split'          # editions to different keys, or some stay: refuse
EMPTY = 'empty'          # no editions bound: refuse


def classify(old_key, new_keys):
    """The outcome for one configuration from its key and the keys its
    editions would derive."""
    keys = set(new_keys)
    if not keys:
        return EMPTY
    if keys == {old_key}:
        return UNCHANGED
    if len(keys) == 1:
        return MOVES
    return SPLIT


def plan_problems(plan, expect_editions=None):
    """Why the plan may not be applied, as text lines; empty when it may.
    ``plan`` is plan_rebind's result."""
    problems = []
    named = {entry['pc'] for entry in plan}
    for entry in plan:
        label = entry['pc']
        if entry['target'] and entry['target']['label'] in named:
            problems.append(f"{label}: target {entry['target']['label']} is "
                            'itself being superseded')
        if entry['outcome'] != MOVES:
            problems.append(f"{label}: {entry['outcome']}, not one whole move")
        if entry['lifecycle'] != 'active':
            problems.append(f"{label}: lifecycle {entry['lifecycle']}, not active")
        if entry['requestors'] or entry['metadata']:
            problems.append(f'{label}: carries requestors or metadata; '
                            'move them through the services setters first')
        target = entry['target']
        if target and target['lifecycle'] not in ('', 'active'):
            problems.append(f"{label}: target {target['label']} is "
                            f"{target['lifecycle']}, not active")
    total = sum(len(entry['editions']) for entry in plan)
    if expect_editions is not None and total != expect_editions:
        problems.append(f'{total} editions bound, {expect_editions} expected')
    return problems


def plan_rebind(pc_labels):
    """What resaving the editions of the named configurations would bind,
    without writing. One entry per label, in the order given."""
    from .models import Dataset, PhysicsConfig, ProdTask
    from .physics_config import _source_path, config_name, physics_config_key

    plan = []
    for label in pc_labels:
        pc = PhysicsConfig.objects.select_related('physics_tag').get(label=label)
        editions = []
        rows = (Dataset.objects.filter(physics_config=pc)
                .select_related('physics_tag', 'evgen_tag', 'background_tag', 'campaign')
                .order_by('pk'))
        for ds in rows:
            key = config_name(physics_config_key(ds))
            existing = PhysicsConfig.objects.filter(config_key=key).first()
            editions.append({
                'pk': ds.pk,
                'composed_name': ds.composed_name,
                'stage': (ds.metadata or {}).get('stage', ''),
                'campaign': ds.campaign.name if ds.campaign_id else '',
                'source': _source_path(ds),
                'tasks': list(ProdTask.objects.filter(dataset=ds)
                              .order_by('pk').values_list('name', flat=True)),
                'new_key': key,
                'new_pc': existing.label if existing else '',
                'new_pc_lifecycle': existing.lifecycle if existing else '',
            })
        new_keys = [e['new_key'] for e in editions]
        outcome = classify(pc.config_key, new_keys)
        target = None
        if outcome == MOVES:
            first = editions[0]
            target = {'key': first['new_key'], 'label': first['new_pc'],
                      'lifecycle': first['new_pc_lifecycle']}
        plan.append({
            'pc': pc.label, 'key': pc.config_key, 'lifecycle': pc.lifecycle,
            'requestors': pc.requestors or [], 'metadata': pc.metadata or {},
            'x_range': (pc.physics_tag.parameters or {}).get('x_range', ''),
            'physics_tag': pc.physics_tag.tag_label,
            'outcome': outcome, 'target': target, 'editions': editions,
        })
    return plan


def lock_scope(pc_labels):
    """Lock the named configurations, then every edition bound to them,
    each in primary-key order, for the rest of the enclosing transaction.
    Returns the locked configurations by label; refuses an unknown label."""
    from django.core.exceptions import ValidationError
    from .models import Dataset, PhysicsConfig

    configs = list(PhysicsConfig.objects.select_for_update()
                   .filter(label__in=pc_labels).order_by('pk'))
    missing = sorted(set(pc_labels) - {pc.label for pc in configs})
    if missing:
        raise ValidationError(f"unknown configuration(s): {', '.join(missing)}")
    list(Dataset.objects.select_for_update().filter(physics_config__in=configs)
         .order_by('pk').values_list('pk', flat=True))
    return {pc.label: pc for pc in configs}


def lock_targets(plan):
    """Lock the existing target configurations in primary-key order and
    return why any may not receive editions, as text lines."""
    from .models import PhysicsConfig

    keys = {entry['target']['key'] for entry in plan if entry['target']}
    problems = []
    for pc in (PhysicsConfig.objects.select_for_update()
               .filter(config_key__in=keys).order_by('pk')):
        if pc.lifecycle != 'active':
            problems.append(f'target {pc.label} is {pc.lifecycle}, not active')
    return problems


def apply_rebind(pc_labels, *, actor, reason, expect_editions):
    """Lock the scope, plan again under the locks, then resave every
    planned edition and supersede each old configuration with its target,
    all in one transaction. Refuses (nothing written) when plan_problems
    or lock_targets report a problem, an edition does not bind as planned,
    or an edition is left on an old configuration. Returns
    (plan, {old label: new label})."""
    from django.core.exceptions import ValidationError
    from .models import Dataset, PhysicsConfig
    from .permanence import identity_change, set_lifecycle

    if not actor or not reason:
        raise ValidationError('An acting person and a reason are required.')
    moved = {}
    with identity_change(actor, reason):
        locked = lock_scope(pc_labels)
        plan = plan_rebind(pc_labels)
        problems = plan_problems(plan, expect_editions) + lock_targets(plan)
        if problems:
            raise ValidationError(problems)
        for entry in plan:
            target_key = entry['target']['key']
            for planned in entry['editions']:
                ds = Dataset.objects.get(pk=planned['pk'])
                ds.save()
                ds.refresh_from_db()
                if ds.physics_config.config_key != target_key:
                    raise ValidationError(
                        f"edition {ds.pk} bound {ds.physics_config.config_key}, "
                        f"planned {target_key}")
                if ds.composed_name != planned['composed_name']:
                    raise ValidationError(
                        f'edition {ds.pk} renamed {planned["composed_name"]} '
                        f'-> {ds.composed_name}')
            old = locked[entry['pc']]
            left = list(Dataset.objects.filter(physics_config=old)
                        .order_by('pk').values_list('pk', flat=True))
            if left:
                raise ValidationError(
                    f"{old.label}: editions {left} still bound; not superseded")
            new = PhysicsConfig.objects.get(config_key=target_key)
            set_lifecycle(old, 'superseded', reason=reason, changed_by=actor,
                          replacement=new)
            moved[old.label] = new.label
    return plan, moved
