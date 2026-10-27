"""Resave the editions of named configurations after a derivation
correction, and supersede each old configuration (pcs.edition_repair).
Dry run unless --apply."""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from pcs.edition_repair import apply_rebind, plan_problems, plan_rebind


class Command(BaseCommand):
    help = ('Rebind exactly the editions of the named physics configurations to the '
            'configuration their corrected identity derives; supersede the old ones. '
            'Dry run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--pc', action='append', required=True, dest='pcs',
                            help='Physics configuration label (repeat for each).')
        parser.add_argument('--expect-editions', type=int,
                            help='The number of editions bound to the named '
                                 'configurations; required with --apply.')
        parser.add_argument('--apply', action='store_true',
                            help='Write. Without it nothing is written.')
        parser.add_argument('--actor', default='',
                            help='The acting person (login); required with --apply.')
        parser.add_argument('--reason', default='',
                            help='Recorded with every change; required with --apply.')

    def handle(self, **options):
        labels = list(dict.fromkeys(options['pcs']))
        try:
            plan = plan_rebind(labels)
        except Exception as e:  # an unknown label names nothing to repair
            raise CommandError(str(e))
        self._report(plan)
        problems = plan_problems(plan, options['expect_editions'])
        for line in problems:
            self.stdout.write(self.style.WARNING(f'refused: {line}'))
        if not options['apply']:
            self.stdout.write('Dry run: nothing written.')
            return
        if options['expect_editions'] is None or not options['actor'] or not options['reason']:
            raise CommandError('--apply needs --expect-editions, --actor and --reason.')
        if problems:
            raise CommandError('Not applied: the plan has problems (above).')
        try:
            _, moved = apply_rebind(labels, actor=options['actor'],
                                    reason=options['reason'],
                                    expect_editions=options['expect_editions'])
        except ValidationError as e:
            raise CommandError('Not applied: ' + '; '.join(e.messages))
        for old, new in moved.items():
            self.stdout.write(self.style.SUCCESS(f'{old} superseded by {new}'))

    def _report(self, plan):
        out = self.stdout.write
        for entry in plan:
            target = entry['target']
            to = ''
            if target:
                to = f" -> {target['label'] or '(new pc)'} {target['key']}"
            out(f"{entry['pc']} [{entry['lifecycle']}] {entry['key']}: "
                f"{entry['outcome']}{to}")
            out(f"  physics tag {entry['physics_tag']} x_range="
                f"{entry['x_range'] or '(none)'}; requestors={entry['requestors']} "
                f"metadata={'set' if entry['metadata'] else '{}'}")
            for e in entry['editions']:
                out(f"  edition {e['pk']} {e['stage'] or '-'} "
                    f"{e['campaign'] or '-'} {e['composed_name']}")
                out(f"    source {e['source'] or '(none)'}")
                out(f"    tasks {', '.join(e['tasks']) or '(none)'}")
                out(f"    derives {e['new_key']} "
                    f"({e['new_pc'] or 'new pc'}{' ' + e['new_pc_lifecycle'] if e['new_pc'] else ''})")
        total = sum(len(entry['editions']) for entry in plan)
        out(f'{len(plan)} configurations, {total} editions.')
