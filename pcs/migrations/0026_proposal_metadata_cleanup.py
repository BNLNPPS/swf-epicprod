"""Fold pre-ledger proposal metadata into the Proposal table.

The proposal ledger (0025) is canonical; dataset metadata carries only a
render projection of a pending proposal. Convert any pending
``metadata['proposal']`` block into a ledger row (preserving it), and any
legacy ``metadata['proposal_denied']`` denial-memory block into a denied
ledger row, then drop the legacy key. Forward-only.
"""

from django.db import migrations


def fold_metadata_proposals(apps, schema_editor):
    Dataset = apps.get_model('pcs', 'Dataset')
    Proposal = apps.get_model('pcs', 'Proposal')

    for ds in Dataset.objects.filter(metadata__has_key='proposal'):
        md = dict(ds.metadata or {})
        p = md.get('proposal') or {}
        if p and not p.get('id'):
            row = Proposal.objects.create(
                action=p.get('action', 'propagation'),
                subject_type='dataset',
                subject_key=ds.composed_name,
                payload=p.get('payload') or {},
                comment=p.get('comment', ''),
                proposer=p.get('proposer', ''),
                scan_version=p.get('scan_version') or 1,
                batch_id=p.get('batch_id', ''),
                executor='service',
                precondition={'prev_state': p.get('prev_state', '')},
                input_hash=p.get('input_hash', ''),
                created_by='metadata_migration',
            )
            p['id'] = row.id
            md['proposal'] = p
            ds.metadata = md
            ds.save(update_fields=['metadata'])

    for ds in Dataset.objects.filter(metadata__has_key='proposal_denied'):
        md = dict(ds.metadata or {})
        legacy = md.pop('proposal_denied') or {}
        Proposal.objects.create(
            action='propagation',
            subject_type='dataset',
            subject_key=ds.composed_name,
            payload={},
            comment='(denial memory migrated from record metadata)',
            proposer='',
            executor='service',
            precondition={},
            input_hash=legacy.get('input_hash', ''),
            status='denied',
            decided_by=legacy.get('denied_by', ''),
            created_by='metadata_migration',
        )
        ds.metadata = md
        ds.save(update_fields=['metadata'])


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0025_proposal_ledger'),
    ]

    operations = [
        migrations.RunPython(fold_metadata_proposals,
                             migrations.RunPython.noop),
    ]
