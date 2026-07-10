"""
Seed PCS tags from the 26.02.0 production campaign.

Idempotent: skips records that already exist. Safe to run multiple times.
Source: https://eic.github.io/epic-prod/FULL/26.02.0/ and RECO/26.02.0/
"""
from django.core.management.base import BaseCommand
from pcs.models import PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag

CREATED_BY = 'srahman1'

CATEGORIES = [
    (1, 'DIS'),
    (2, 'DVCS'),
    (3, 'SIDIS'),
    (4, 'EXCLUSIVE'),
]

EVGEN_TAGS = [
    # (number, generator, generator_version, bg_tag_prefix, description)
    (1, 'pythia8', '8.310', '', 'pythia8 8.310, no backgrounds'),
    (2, 'EpIC', 'v1.1.6-1.2', '', 'EpIC v1.1.6-1.2, no backgrounds'),
    (3, 'EpIC', '1.1.6-1.0', '', 'EpIC 1.1.6-1.0, no backgrounds'),
    (4, 'BeAGLE', '1.03.02-1.2', '', 'BeAGLE 1.03.02-1.2, no backgrounds'),
    (5, 'BeAGLE', '1.03.02-2.0', '', 'BeAGLE 1.03.02-2.0, no backgrounds'),
    (6, 'BeAGLE', '1.03.02-1.1', '', 'BeAGLE 1.03.02-1.1, no backgrounds'),
    (7, 'eSTARlight', '1.3.0-1.0', '', 'eSTARlight 1.3.0-1.0, no backgrounds'),
    (8, 'sartre', '1.39-1.1', '', 'sartre 1.39-1.1, no backgrounds'),
    (9, 'DEMPgen', '1.2.4', '', 'DEMPgen 1.2.4, no backgrounds'),
    (10, 'lAger', '3.6.1-1.0', '', 'lAger 3.6.1-1.0, no backgrounds'),
    (11, 'rapgap', '3.310-1.0', '', 'rapgap 3.310-1.0, no backgrounds'),
    (12, 'EpIC', 'v1.1.6-1.2', 'Bkg_Exact1S_2us/GoldCt/5um',
     'EpIC v1.1.6-1.2, synrad+brems bg, gold 5um'),
    (13, 'EpIC', 'v1.1.6-1.2', 'Bkg_Exact1S_2us/GoldCt/10um',
     'EpIC v1.1.6-1.2, synrad+brems bg, gold 10um'),
    (14, 'pythia8', '8.306-1.0', '', 'pythia8 8.306-1.0, no backgrounds'),
    (15, 'pythia8', '8.306-1.1', '', 'pythia8 8.306-1.1, no backgrounds'),
]

# Physics tags: (category_digit, process, e_beam, h_beam, species, extra_params, description)
# extra_params is a dict merged into parameters
PHYSICS_TAGS = [
    # --- Cat 1: DIS ---
    # DIS_NC ep standard
    (1, 'DIS_NC', '5', '41', 'ep', {'q2_range': 'minQ2=1'}, 'DIS NC 5x41 ep minQ2=1'),
    (1, 'DIS_NC', '5', '100', 'ep', {'q2_range': 'minQ2=1'}, 'DIS NC 5x100 ep minQ2=1'),
    (1, 'DIS_NC', '10', '100', 'ep', {'q2_range': 'minQ2=1'}, 'DIS NC 10x100 ep minQ2=1'),
    (1, 'DIS_NC', '10', '275', 'ep', {'q2_range': 'minQ2=1'}, 'DIS NC 10x275 ep minQ2=1'),
    (1, 'DIS_NC', '18', '275', 'ep', {'q2_range': 'minQ2=1'}, 'DIS NC 18x275 ep minQ2=1'),
    # DIS_NC ep higher Q2
    (1, 'DIS_NC', '10', '100', 'ep', {'q2_range': 'minQ2=10'}, 'DIS NC 10x100 ep minQ2=10'),
    (1, 'DIS_NC', '10', '100', 'ep', {'q2_range': 'minQ2=100'}, 'DIS NC 10x100 ep minQ2=100'),
    (1, 'DIS_NC', '10', '100', 'ep', {'q2_range': 'minQ2=1000'}, 'DIS NC 10x100 ep minQ2=1000'),
    # DIS_CC ep
    (1, 'DIS_CC', '10', '100', 'ep', {'q2_range': 'minQ2=100'}, 'DIS CC 10x100 ep minQ2=100'),
    (1, 'DIS_CC', '10', '275', 'ep', {'q2_range': 'minQ2=100'}, 'DIS CC 10x275 ep minQ2=100'),
    (1, 'DIS_CC', '18', '275', 'ep', {'q2_range': 'minQ2=100'}, 'DIS CC 18x275 ep minQ2=100'),
    # DDIS
    (1, 'DDIS', '10', '100', 'ep', {}, 'DDIS 10x100 ep'),
    # DIS_NC eAu
    (1, 'DIS_NC', '10', '110', 'eAu', {'q2_range': 'minQ2=1'}, 'DIS NC 10x110 eAu minQ2=1'),
    (1, 'DIS_NC', '18', '110', 'eAu', {'q2_range': 'minQ2=1'}, 'DIS NC 18x110 eAu minQ2=1'),
    # DIS_NC eHe3
    (1, 'DIS_NC', '10', '130', 'eHe3', {'q2_range': 'minQ2=1'}, 'DIS NC 10x130 eHe3 minQ2=1'),
    (1, 'DIS_NC', '18', '130', 'eHe3', {'q2_range': 'minQ2=1'}, 'DIS NC 18x130 eHe3 minQ2=1'),

    # --- Cat 2: DVCS ---
    (2, 'DVCS', '5', '41', 'ep', {}, 'DVCS 5x41 ep'),
    (2, 'DVCS', '5', '100', 'ep', {}, 'DVCS 5x100 ep'),
    (2, 'DVCS', '10', '100', 'ep', {}, 'DVCS 10x100 ep'),
    (2, 'DVCS', '10', '275', 'ep', {}, 'DVCS 10x275 ep'),
    (2, 'DVCS', '18', '275', 'ep', {}, 'DVCS 18x275 ep'),
    # DDVCS
    (2, 'DDVCS', '10', '100', 'ep', {'decay_mode': 'edecay', 'hadron_charge': 'hplus'},
     'DDVCS 10x100 ep edecay hplus'),
    (2, 'DDVCS', '10', '100', 'ep', {'decay_mode': 'edecay', 'hadron_charge': 'hminus'},
     'DDVCS 10x100 ep edecay hminus'),
    (2, 'DDVCS', '10', '100', 'ep', {'decay_mode': 'mudecay', 'hadron_charge': 'hplus'},
     'DDVCS 10x100 ep mudecay hplus'),
    (2, 'DDVCS', '10', '100', 'ep', {'decay_mode': 'mudecay', 'hadron_charge': 'hminus'},
     'DDVCS 10x100 ep mudecay hminus'),

    # --- Cat 3: SIDIS ---
    (3, 'SIDIS_D0', '5', '41', 'ep', {}, 'SIDIS D0 5x41 ep'),
    (3, 'SIDIS_D0', '10', '100', 'ep', {}, 'SIDIS D0 10x100 ep'),
    (3, 'SIDIS_D0', '18', '275', 'ep', {}, 'SIDIS D0 18x275 ep'),

    # --- Cat 4: EXCLUSIVE ---
    # DEMP
    (4, 'DEMP', '5', '41', 'ep', {}, 'DEMP 5x41 ep'),
    (4, 'DEMP', '5', '100', 'ep', {}, 'DEMP 5x100 ep'),
    (4, 'DEMP', '10', '100', 'ep', {'q2_range': 'q2_1to10'}, 'DEMP 10x100 ep Q2 1-10'),
    (4, 'DEMP', '10', '100', 'ep', {'q2_range': 'q2_1to50'}, 'DEMP 10x100 ep Q2 1-50'),
    (4, 'DEMP', '10', '100', 'ep', {'q2_range': 'q2_1to10000'}, 'DEMP 10x100 ep Q2 1-10000'),
    (4, 'DEMP', '10', '100', 'ep', {'q2_range': 'q2_2to10'}, 'DEMP 10x100 ep Q2 2-10'),
    # DVMP
    (4, 'DVMP', '5', '41', 'ep', {}, 'DVMP 5x41 ep'),
    (4, 'DVMP', '10', '100', 'ep', {}, 'DVMP 10x100 ep'),
    (4, 'DVMP', '18', '275', 'ep', {}, 'DVMP 18x275 ep'),
    # DIFFRACTIVE_JPSI
    (4, 'DIFFRACTIVE_JPSI', '5', '41', 'ep', {}, 'Diffractive J/psi 5x41 ep'),
    (4, 'DIFFRACTIVE_JPSI', '5', '100', 'ep', {}, 'Diffractive J/psi 5x100 ep'),
    (4, 'DIFFRACTIVE_JPSI', '10', '100', 'ep', {}, 'Diffractive J/psi 10x100 ep'),
    (4, 'DIFFRACTIVE_JPSI', '10', '275', 'ep', {}, 'Diffractive J/psi 10x275 ep'),
    (4, 'DIFFRACTIVE_JPSI', '18', '275', 'ep', {}, 'Diffractive J/psi 18x275 ep'),
    # DIFFRACTIVE_PHI
    (4, 'DIFFRACTIVE_PHI', '10', '100', 'ep', {}, 'Diffractive phi 10x100 ep'),
    # DIFFRACTIVE_RHO
    (4, 'DIFFRACTIVE_RHO', '10', '100', 'ep', {'coherence': 'coherent'},
     'Diffractive rho 10x100 ep coherent'),
    # PHOTOPRODUCTION_JPSI
    (4, 'PHOTOPRODUCTION_JPSI', '18', '275', 'ep', {'decay_mode': 'edecay'},
     'Photoproduction J/psi 18x275 ep edecay'),
    (4, 'PHOTOPRODUCTION_JPSI', '18', '275', 'ep', {'decay_mode': 'mudecay'},
     'Photoproduction J/psi 18x275 ep mudecay'),
    # UPSILON
    (4, 'UPSILON', '18', '275', 'ep', {}, 'Upsilon 18x275 ep'),
]


class Command(BaseCommand):
    help = 'Seed PCS tags from the 26.02.0 production campaign'

    def handle(self, *args, **options):
        counts = {'categories': 0, 'evgen': 0, 'simu': 0, 'reco': 0, 'physics': 0}
        skipped = {'categories': 0, 'evgen': 0, 'simu': 0, 'reco': 0, 'physics': 0}

        # Categories
        for digit, name in CATEGORIES:
            _, created = PhysicsCategory.objects.get_or_create(
                digit=digit, defaults={'name': name, 'created_by': CREATED_BY}
            )
            if created:
                counts['categories'] += 1
            else:
                skipped['categories'] += 1

        # EvGen tags
        for num, gen, ver, bg, desc in EVGEN_TAGS:
            if EvgenTag.objects.filter(tag_number=num).exists():
                skipped['evgen'] += 1
                continue
            params = {'generator': gen, 'generator_version': ver}
            if bg:
                params['bg_tag_prefix'] = bg
            EvgenTag.objects.create(
                tag_number=num, description=desc, parameters=params,
                created_by=CREATED_BY
            )
            counts['evgen'] += 1

        # Simu tag
        if SimuTag.objects.filter(tag_number=1).exists():
            skipped['simu'] += 1
        else:
            SimuTag.objects.create(
                tag_number=1,
                description='npsim 26.02.0, standard filters',
                parameters={'detector_sim': 'npsim', 'sim_version': '26.02.0'},
                created_by=CREATED_BY,
            )
            counts['simu'] += 1

        # Reco tag
        if RecoTag.objects.filter(tag_number=1).exists():
            skipped['reco'] += 1
        else:
            RecoTag.objects.create(
                tag_number=1,
                description='eicrecon 26.02.0, standard',
                parameters={'reco_version': '26.02.0', 'reco_config': 'standard'},
                created_by=CREATED_BY,
            )
            counts['reco'] += 1

        # Physics tags
        for cat_digit, process, e_beam, h_beam, species, extra, desc in PHYSICS_TAGS:
            category = PhysicsCategory.objects.get(digit=cat_digit)
            params = {
                'process': process,
                'beam_energy_electron': e_beam,
                'beam_energy_hadron': h_beam,
                'beam_species': species,
            }
            params.update(extra)
            # Check for duplicate by matching all parameters in this category
            dup = PhysicsTag.objects.filter(category=category, parameters=params).exists()
            if dup:
                skipped['physics'] += 1
                continue
            tag_number = PhysicsTag.allocate_next(category)
            PhysicsTag.objects.create(
                tag_number=tag_number, category=category, description=desc,
                parameters=params, created_by=CREATED_BY,
            )
            counts['physics'] += 1

        # Update PersistentState for evgen/simu/reco counters
        from monitor_app.models import PersistentState
        from django.db import transaction
        with transaction.atomic():
            obj, _ = PersistentState.objects.select_for_update().get_or_create(
                id=1, defaults={'state_data': {}}
            )
            max_evgen = EvgenTag.objects.order_by('-tag_number').values_list(
                'tag_number', flat=True).first() or 0
            max_simu = SimuTag.objects.order_by('-tag_number').values_list(
                'tag_number', flat=True).first() or 0
            max_reco = RecoTag.objects.order_by('-tag_number').values_list(
                'tag_number', flat=True).first() or 0
            # Physics: max suffix (tag_number % 1000) across all categories
            max_suffix = 0
            for pt in PhysicsTag.objects.all():
                suffix = pt.tag_number % 1000
                if suffix > max_suffix:
                    max_suffix = suffix
            obj.state_data['pcs_next_physics'] = max_suffix + 1
            obj.state_data['pcs_next_evgen'] = max_evgen + 1
            obj.state_data['pcs_next_simu'] = max_simu + 1
            obj.state_data['pcs_next_reco'] = max_reco + 1
            obj.save()

        self.stdout.write(self.style.SUCCESS(
            f"Created: {counts['categories']} categories, {counts['evgen']} evgen, "
            f"{counts['simu']} simu, {counts['reco']} reco, {counts['physics']} physics"
        ))
        self.stdout.write(
            f"Skipped: {skipped['categories']} categories, {skipped['evgen']} evgen, "
            f"{skipped['simu']} simu, {skipped['reco']} reco, {skipped['physics']} physics"
        )
        total = sum(counts.values())
        self.stdout.write(self.style.SUCCESS(f"Total created: {total}"))
