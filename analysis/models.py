"""Analysis runs on production samples (docs/ANALYSIS.md).

Analysis is its own domain on the platform (ARCHITECTURE_MAP.md), so it
is its own application here rather than more of PCS. It reads PCS through
two links only: a dataset card names the PCS Dataset whose delivered
files it froze, and nothing in PCS knows about analysis.

Four records, after the production pattern (tag = permanent identity,
task = unit of work):

- AnalysisTag  a1, a2, ...: an analysis card at a pinned commit.
- DatasetCard  one sample's frozen file list at one revision.
- AnalysisRun  a tag on dataset cards, minus exclusions, with
               parameters and a split; frozen into a run card whose
               sha256 is the run's identity.
- AnalysisJobOutput  one map job's partial result, as its report gave it.
"""
from django.db import models, transaction

from . import cards


class AnalysisTag(models.Model):
    """An analysis card at a pinned commit. Permanent: once a run uses it,
    nothing that changes what runs may change; a new commit is a new tag.
    The card is stored verbatim with its digest, so the record says what
    ran even if the repository moves."""
    tag_number = models.PositiveIntegerField(unique=True)
    repo = models.URLField(max_length=300,
                           help_text='Repository URL, e.g. https://github.com/eic/physics_benchmarks')
    commit = models.CharField(max_length=40)
    card_path = models.CharField(max_length=300,
                                 help_text='Path of the analysis card in the repository')
    card = models.JSONField(help_text='The analysis card as read at the commit')
    card_text = models.TextField(help_text='The card file, byte for byte')
    card_sha256 = models.CharField(max_length=64)
    image = models.CharField(max_length=300,
                             help_text='The container the jobs run, as submitted')
    description = models.TextField(blank=True, default='')
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    #: Fields a run's record depends on; frozen once a run uses the tag.
    PINNED = ('repo', 'commit', 'card_path', 'card', 'card_text',
              'card_sha256', 'image')

    class Meta:
        db_table = 'analysis_tag'
        ordering = ['-tag_number']
        constraints = [
            models.UniqueConstraint(fields=['repo', 'commit', 'card_path', 'image'],
                                    name='analysis_tag_unique_pin'),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return f'a{self.tag_number}'

    @property
    def in_use(self):
        return self.pk is not None and self.runs.exists()

    def pin(self):
        """The tag as a run card names it."""
        return {'label': self.label, 'repo': self.repo, 'commit': self.commit,
                'card_path': self.card_path, 'card_sha256': self.card_sha256,
                'image': self.image}

    def save(self, *args, **kwargs):
        if self.pk is not None and self.in_use:
            stored = type(self).objects.get(pk=self.pk)
            changed = [f for f in self.PINNED if getattr(stored, f) != getattr(self, f)]
            if changed:
                raise cards.CardError(
                    f'{self.label} is used by a run; {", ".join(changed)} cannot change '
                    '(register a new tag instead)')
        if self.tag_number is None:
            with transaction.atomic():
                last = (type(self).objects.select_for_update()
                        .order_by('-tag_number').values_list('tag_number', flat=True).first())
                self.tag_number = (last or 0) + 1
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)


class DatasetCard(models.Model):
    """One sample's frozen file list at one revision: one file per work
    unit, the latest delivered try. Never edited; a new delivery makes a
    new revision. Every analysis reading that sample at that revision
    reads the same files."""
    SOURCE_CHOICES = [
        ('delivered', 'PCS delivered outputs'),
        ('rucio', 'Rucio listing'),
    ]
    dataset = models.ForeignKey('pcs.Dataset', on_delete=models.PROTECT,
                                related_name='analysis_dataset_cards')
    sample = models.CharField(max_length=400, db_index=True,
                              help_text='RECO/<version>/<config>/... without its try segment')
    revision = models.PositiveIntegerField()
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    file_count = models.PositiveIntegerField()
    events = models.BigIntegerField(null=True, blank=True)
    manifest_sha256 = models.CharField(max_length=64)
    manifest = models.BinaryField(help_text='gzipped CSV, cards.manifest_blob')
    header = models.JSONField(help_text='cards.dataset_card')
    frozen_at = models.DateTimeField()
    frozen_by = models.CharField(max_length=100)

    class Meta:
        db_table = 'analysis_dataset_card'
        ordering = ['sample', '-revision']
        constraints = [
            models.UniqueConstraint(fields=['sample', 'revision'],
                                    name='analysis_dataset_card_unique_revision'),
        ]

    def __str__(self):
        return f'{self.sample} r{self.revision}'

    def rows(self):
        return cards.read_manifest_blob(self.manifest)


class AnalysisRun(models.Model):
    """A tag on dataset cards. A draft can be edited; freezing writes the
    run card and its id, after which nothing that changes the result can
    change. Queue, memory, purpose and owner stay out of the run card:
    promoting a user's run to validation republishes the same id."""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('frozen', 'Frozen'),
        ('submitted', 'Submitted'),
        ('merging', 'Merging'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]
    PURPOSE_CHOICES = [(p, p.capitalize()) for p in cards.PURPOSES]

    tag = models.ForeignKey(AnalysisTag, on_delete=models.PROTECT, related_name='runs')
    inputs = models.ManyToManyField(DatasetCard, related_name='runs')
    exclusions = models.JSONField(default=list, blank=True,
                                  help_text='[{"did": ..., "reason": ...}]')
    parameters = models.JSONField(default=dict, blank=True,
                                  help_text='Overrides of the card\'s parameters')
    files_per_job = models.PositiveIntegerField(
        null=True, blank=True, help_text='Overrides the card\'s split.files_per_job')
    purpose = models.CharField(max_length=16, choices=PURPOSE_CHOICES, default='user')
    owner = models.CharField(max_length=100)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='draft',
                              db_index=True)
    run_card = models.JSONField(null=True, blank=True)
    run_card_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    campaign = models.CharField(max_length=20, blank=True, default='')
    # The PanDA side, outside the run card: queue and memory change where a
    # run runs, not what it computes.
    site = models.CharField(max_length=100, blank=True, default='BNL_PanDA_1')
    memory_mb = models.PositiveIntegerField(default=2000)
    walltime_hours = models.FloatField(default=4.0)
    panda_tasks = models.JSONField(default=list, blank=True,
                                   help_text='[{"stage", "jedi_task_id", "try"}]')
    result = models.JSONField(null=True, blank=True,
                              help_text='The result card, written at publish')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analysis_run'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.tag.label} {self.run_card_id[:12] if self.run_card_id else "draft"}'

    @property
    def is_frozen(self):
        return self.status != 'draft'


class AnalysisJobOutput(models.Model):
    """One map job's partial result in the run's scratch area, from the
    job's report. These rows are the merge job's input list; it never
    lists the directory."""
    STATUS_CHOICES = [
        ('written', 'Written'),
        ('lost', 'Lost'),
        ('merged', 'Merged'),
    ]
    run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name='job_outputs')
    seq = models.PositiveIntegerField(help_text='The job\'s manifest row')
    pandaid = models.BigIntegerField(null=True, blank=True)
    did = models.CharField(max_length=500, help_text='Unregistered DID name of the partial')
    adler32 = models.CharField(max_length=8, blank=True, default='')
    bytes = models.BigIntegerField(null=True, blank=True)
    files_read = models.PositiveIntegerField(null=True, blank=True)
    events_read = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='written')
    reason = models.TextField(blank=True, default='')
    reported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'analysis_job_output'
        ordering = ['run', 'seq']
        constraints = [
            models.UniqueConstraint(fields=['run', 'seq'], name='analysis_job_output_unique_seq'),
        ]

    def __str__(self):
        return f'{self.run} job {self.seq} ({self.status})'
