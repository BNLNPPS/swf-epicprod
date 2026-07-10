"""
PCS (Physics Configuration System) data models.

Tag lifecycle: draft (editable) → locked (immutable, usable in datasets).
Tag numbering: physics tags = category.digit * 1000 + N; e/s/r tags increment from 1 via PersistentState.
Datasets: composed from four tags (plus optional background), auto-named, with block management for Rucio's 100k file limit.
"""
from django.db import models, transaction
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.utils.functional import cached_property

from .name_tokens import sample_name_reserved_collision, reserved_sample_token_description


TAG_STATUS_CHOICES = [
    ('draft', 'Draft'),
    ('locked', 'Locked'),
]


class PhysicsCategory(models.Model):
    """Physics area (e.g. DVCS=3, DIS=4). Digit determines tag number range."""
    digit = models.PositiveSmallIntegerField(
        primary_key=True,
        validators=[MinValueValidator(1), MaxValueValidator(9)],
        help_text="Single digit 1-9. Physics tag numbers = digit * 1000 + N."
    )
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pcs_physics_category'
        ordering = ['digit']
        verbose_name_plural = 'physics categories'

    def __str__(self):
        return f"{self.digit}: {self.name}"


class PhysicsTag(models.Model):
    """Physics process tag (p3001, p3002...). Number = category.digit * 1000 + N."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    category = models.ForeignKey(
        PhysicsCategory, on_delete=models.PROTECT, related_name='tags'
    )
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_physics_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"p{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls, category):
        """Atomically allocate the next tag number: category.digit * 1000 + global suffix."""
        suffix = _allocate_simple_tag('pcs_next_physics')
        return category.digit * 1000 + suffix


class EvgenTag(models.Model):
    """Event generation tag (e1, e2...). Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_evgen_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"e{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_evgen')


class SimuTag(models.Model):
    """Simulation tag (s1, s2...). Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_simu_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"s{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_simu')


class RecoTag(models.Model):
    """Reconstruction tag (r1, r2...). Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_reco_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"r{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_reco')


class BackgroundTag(models.Model):
    """Background tag (k1, k2...). A named, versioned background configuration
    (beam-gas, synchrotron radiation, or overlay samples), independent of any
    physics signal. Number auto-incremented via PersistentState."""
    tag_number = models.IntegerField(unique=True)
    tag_label = models.CharField(max_length=10, unique=True)
    status = models.CharField(max_length=10, choices=TAG_STATUS_CHOICES, default='draft')
    description = models.TextField(blank=True, default='')
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_background_tag'
        ordering = ['tag_number']

    def __str__(self):
        return self.tag_label

    def save(self, *args, **kwargs):
        self.tag_label = f"k{self.tag_number}"
        super().save(*args, **kwargs)

    @classmethod
    def allocate_next(cls):
        return _allocate_simple_tag('pcs_next_background')


class Dataset(models.Model):
    """
    Production dataset composed from four tags plus an optional background.

    Each row is one block. Block 1 always exists. The dataset_name ties
    blocks together. The DID format is '{scope}:{dataset_name}.b{N}'.
    Tags may be draft during alpha; reproducibility locking is enforced at
    submission prep, not at composition.
    """
    dataset_name = models.CharField(max_length=255)
    # The composed (tag-based) identity, stored and kept current on every save
    # (see save() / build_dataset_name). This is the canonical name used in URLs,
    # the API, and the PanDA taskName / Rucio DID; it is read directly and
    # indexed for resolution, never rebuilt from the tag FKs at read time. The
    # legacy ``dataset_name`` above (a csv_import.<hash> for imported rows) is
    # left untouched. Not unique: block rows (b1, b2, …) share a composed name.
    composed_name = models.CharField(max_length=255, db_index=True, blank=True, default='')
    scope = models.CharField(max_length=100, default='group.EIC')
    detector_version = models.CharField(max_length=50)
    detector_config = models.CharField(max_length=100)
    # Optional bookkeeping link to the production campaign that imported or owns
    # this row. It is not part of the dataset identity; detector_version is the
    # version segment because it describes the conditions of the produced data.
    campaign = models.ForeignKey('Campaign', on_delete=models.PROTECT,
                                 related_name='datasets', null=True, blank=True)
    physics_tag = models.ForeignKey(PhysicsTag, on_delete=models.PROTECT, related_name='datasets')
    evgen_tag = models.ForeignKey(EvgenTag, on_delete=models.PROTECT, related_name='datasets')
    simu_tag = models.ForeignKey(SimuTag, on_delete=models.PROTECT, related_name='datasets')
    reco_tag = models.ForeignKey(RecoTag, on_delete=models.PROTECT, related_name='datasets')
    background_tag = models.ForeignKey(
        BackgroundTag, on_delete=models.PROTECT, related_name='datasets',
        null=True, blank=True,
    )
    # Sample-variant discriminator: the trailing identity segment that tells
    # apart datasets sharing one tag composition (e.g. the single-particle
    # angular range '130to177deg'). It is a production discriminator, not a
    # physics parameter, so it is not a tag; it composes into the dataset name
    # after the tag run. Empty when the tags alone are a unique identity.
    sample_name = models.CharField(max_length=120, blank=True, default='')
    # Cross-campaign propagation disposition of THIS campaign edition of the
    # sample. The composed identity is campaign-specific through its version
    # segment; the version-less tag composition + sample name is the family.
    # Consumed at next-campaign creation: 'continue' editions mint successor
    # editions, 'hold' stays in the catalog without next-campaign production,
    # 'final' produces this campaign and ends the family. Operators (or an
    # operator approving an AI proposal) flip states; ingest never does.
    PROPAGATION_CHOICES = [
        ('continue', 'continue'), ('hold', 'hold'), ('final', 'final'),
    ]
    propagation = models.CharField(
        max_length=16, choices=PROPAGATION_CHOICES, default='continue')
    # Composed name of the successor family when a retirement has a designated
    # replacement (campaign-level changes such as an energy migration). A name
    # reference, not an FK: the successor may not be materialized yet.
    replaced_by = models.CharField(max_length=255, blank=True, default='')
    block_num = models.PositiveIntegerField(default=1)
    blocks = models.PositiveIntegerField(default=1)
    did = models.CharField(max_length=300, unique=True)
    file_count = models.IntegerField(default=0)
    data_size = models.BigIntegerField(default=0)
    description = models.TextField(blank=True, default='')
    metadata = models.JSONField(null=True, blank=True)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    EXTERNAL_SOURCE_KINDS = {'csv_manifest', 'path', 'url', 'rucio_did', 'file_list'}

    class Meta:
        db_table = 'pcs_dataset'
        ordering = ['-created_at']
        unique_together = [('dataset_name', 'block_num')]

    def __str__(self):
        return self.did

    def get_metadata(self):
        """Return metadata as a mutable dict, normalizing empty/null values."""
        return self.metadata if isinstance(self.metadata, dict) else {}

    def get_metadata_value(self, *path, default=None):
        data = self.get_metadata()
        for key in path:
            if not isinstance(data, dict) or key not in data:
                return default
            data = data[key]
        return data

    @property
    def stage(self):
        return self.get_metadata_value('stage', default='')

    @property
    def is_external(self):
        source_kind = self.source_kind
        if source_kind:
            return source_kind in self.EXTERNAL_SOURCE_KINDS
        return bool(self.get_metadata_value('external', default=False))

    @property
    def source_kind(self):
        return self.get_metadata_value('source', 'kind', default='')

    @property
    def source_location(self):
        return self.get_metadata_value('source', 'location', default='')

    @property
    def validation_status(self):
        return self.get_metadata_value('validation', 'status', default='')

    @classmethod
    def external_evgen_metadata(
        cls, source_location, source_kind='csv_manifest', **_ignored
    ):
        return {
            'stage': 'evgen',
            'source': {
                'kind': source_kind,
                'location': source_location,
            },
        }

    def clean(self):
        # Draft tags are allowed on datasets during alpha — composition stays
        # editable so ops can fix tag meaning. Reproducibility locking is
        # enforced at submission prep, not here; tightened as we commission.
        # Reserved-token rule (PCS.md §Composed-name suffixes): the sample
        # segment must not collide with optional tag or terminal suffix tokens
        # that anchor positional parsing.
        if sample_name_reserved_collision(self.sample_name):
            raise ValidationError(
                f"sample_name {self.sample_name!r} collides with a reserved "
                f"token ({reserved_sample_token_description()}).")

    def save(self, *args, **kwargs):
        if not self.dataset_name:
            self.dataset_name = self.build_dataset_name()
        if not self.did:
            self.did = f"{self.scope}:{self.dataset_name}.b{self.block_num}"
        # Recompute the stored composed identity on every save so it can never
        # drift from the tag composition, and is read directly at query time.
        self.composed_name = self.build_dataset_name()
        if len(self.dataset_name) > 255:
            raise ValidationError("Dataset name exceeds 255 characters.")
        self.full_clean()
        super().save(*args, **kwargs)

    def build_dataset_name(self):
        """Auto-name: {scope}.{detector_version}.{detector_config}.{p}.{e}.{s}.{r}[.{k}][.{sample_name}]

        The version segment is the detector version because it describes the data
        production conditions. Campaign is bookkeeping and must not move the
        dataset identity. The background segment is appended only when the
        dataset carries a background tag; the sample-variant segment only when
        the dataset carries a sample_name. The sample segment is taken verbatim
        and may contain periods (positional parse, see PCS.md §Sample Variants).
        """
        name = (
            f"{self.scope}.{self.detector_version}.{self.detector_config}"
            f".{self.physics_tag.tag_label}.{self.evgen_tag.tag_label}"
            f".{self.simu_tag.tag_label}.{self.reco_tag.tag_label}"
        )
        if self.background_tag_id:
            name = f"{name}.{self.background_tag.tag_label}"
        if self.sample_name:
            name = f"{name}.{self.sample_name}"
        return name

    @property
    def task_name(self):
        """Task name = dataset_name (without .bN block suffix)."""
        return self.dataset_name

    # composed_name is a stored, indexed column (see the field definition and
    # save()), not a derived property — it is read directly. build_dataset_name()
    # remains the single authority that populates it.

    @property
    def tag_facets(self):
        """Catalog filter facets from the bound physics tag (process, beams, Q2,
        species, energy) plus detector_config — the accurate replacement for
        path/DID-string-derived facets now that every dataset carries its real
        physics tag."""
        p = (self.physics_tag.parameters if self.physics_tag_id else {}) or {}
        e = p.get('beam_energy_electron', '')
        h = p.get('beam_energy_hadron', '')
        beam = f'{e}x{h}' if e and h and e != 'N/A' and h != 'N/A' else ''
        return {
            'detector': self.detector_config or '',
            'beam': beam,
            'physics': p.get('process', ''),
            'q2': p.get('q2_range', ''),
            'species': p.get('beam_species', ''),
            'energy': p.get('gun_energy', ''),
        }

    @property
    def has_internal_name(self):
        """True when dataset_name/DID are legacy-import plumbing — an opaque
        csv_import.<hash> or past.<…> key and a synthetic (non-Rucio) DID —
        rather than a real Rucio identity. Used to keep those strings off the
        UI; the source path is the real-world locator for such rows.
        """
        return self.dataset_name.startswith(('csv_import.', 'past.'))


class ProdConfig(models.Model):
    """
    Production configuration template — mutable operational settings for job submission.

    Captures everything needed to build a production submit command beyond what
    the four tags and dataset already define: background mixing, output control,
    software stack, resource targets, condor template, and PanDA/Rucio overrides.
    Always mutable — the PanDA task/job spec is the immutable record.
    """
    name = models.CharField(max_length=200, unique=True,
                            help_text="Human-readable config name, e.g. 'DVCS 10x100 standard'")
    description = models.TextField(blank=True, default='')

    # Background mixing
    bg_mixing = models.BooleanField(default=False)
    bg_cross_section = models.CharField(max_length=200, blank=True, default='')
    bg_evtgen_file = models.CharField(max_length=500, blank=True, default='')

    # Output file control
    copy_reco = models.BooleanField(default=True)
    copy_full = models.BooleanField(default=False)
    copy_log = models.BooleanField(default=True)
    use_rucio = models.BooleanField(default=True)

    # Software stack
    jug_xl_tag = models.CharField(max_length=100, blank=True, default='',
                                  help_text="e.g. 26.02.0-stable")
    container_image = models.CharField(max_length=500, blank=True, default='',
                                       help_text="Singularity/Apptainer image reference")

    # Resource targets
    target_hours_per_job = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                               help_text="Target walltime per job in hours")
    events_per_task = models.PositiveIntegerField(null=True, blank=True,
                                                  help_text="Total events for the task")

    # Condor template
    condor_template = models.TextField(blank=True, default='',
                                       help_text="HTCondor submission template")

    # PanDA overrides (nullable — PanDA decides defaults)
    panda_site = models.CharField(max_length=200, blank=True, default='')
    panda_queue = models.CharField(max_length=200, blank=True, default='')
    panda_working_group = models.CharField(max_length=100, blank=True, default='')
    panda_resource_type = models.CharField(max_length=100, blank=True, default='')

    # Rucio overrides (nullable)
    rucio_rse = models.CharField(max_length=200, blank=True, default='',
                                 help_text="Rucio Storage Element for output")
    rucio_replication_rules = models.JSONField(null=True, blank=True,
                                               help_text="Rucio replication rule definitions")

    # Extensible submission parameters (no migration needed for new keys).
    # Keys: workflow_mode, transformation, processing_type,
    # prod_source_label, vo, n_jobs, events_per_job, events_per_file,
    # files_per_job, corecount, no_build, skip_scout, exec_command, scope
    data = models.JSONField(null=True, blank=True,
                            help_text="Additional submission parameters (JSON)")

    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_prod_config'
        ordering = ['-updated_at']

    def __str__(self):
        return self.name

    @property
    def workflow_mode(self):
        """Production workflow mode: 'external_evgen' (default) or
        'internal_evgen'. Stored in ``data['workflow_mode']``; defaults
        to 'external_evgen' (current production reality)."""
        return (self.data or {}).get('workflow_mode', 'external_evgen')

    @property
    def submission_path(self):
        """Submission path: 'condor' or 'panda'. Stored in
        ``data['submission_path']``; defaults to 'condor' (current
        production submission path). Whether evgen is run in-house
        or read from input files is a separate axis (workflow_mode)
        and is independent of the submission path."""
        return (self.data or {}).get('submission_path', 'condor')


CAMPAIGN_LIFECYCLE_CHOICES = [
    ('past', 'Past'),
    ('current', 'Current'),
    ('future', 'Future'),
]


class Campaign(models.Model):
    """
    Production campaign — a time-ordered grouping of ProdTasks.

    Lifecycle drives the catalog tabs: past (grey), current (green),
    future (blue). ``set_current`` in the service layer enforces the
    'one current at a time' invariant; no DB constraint, to keep
    transitions painless.
    """
    name = models.CharField(max_length=100, unique=True,
                            help_text="Campaign name, e.g. '26.02.0'")
    lifecycle = models.CharField(max_length=20, default='future')
    description = models.TextField(blank=True, default='')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    clone_of = models.ForeignKey('self', null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name='clones',
                                 help_text="Campaign this was cloned from")
    data = models.JSONField(default=dict, blank=True,
                            help_text="Flexible extension fields (no migration to add new keys)")
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_campaign'
        ordering = ['-start_date', '-created_at']

    def __str__(self):
        return self.name


QUESTIONNAIRE_STATUS_CHOICES = [
    ('active', 'Active'),
    ('review', 'Review'),
    ('linked', 'Linked'),
    ('closed', 'Closed'),
]


class Questionnaire(models.Model):
    """
    Read-only mirror of one ePIC production-request Google Form response.

    Requesters still submit through the Google Form; PCS mirrors the backend
    export so production records can link to the submitted request provenance.
    """
    submitted_at = models.DateTimeField(
        unique=True,
        help_text="Google Form response timestamp; the intake idempotency key",
    )
    description = models.TextField(
        blank=True, default='',
        help_text="Dataset, generator, and purpose as submitted",
    )
    repository = models.TextField(blank=True, default='')
    contact = models.TextField(blank=True, default='')
    nevents = models.TextField(blank=True, default='')
    benchmark = models.TextField(blank=True, default='')
    estimate = models.TextField(blank=True, default='')

    status = models.CharField(
        max_length=20, choices=QUESTIONNAIRE_STATUS_CHOICES, default='active')
    source_url = models.CharField(max_length=500, blank=True, default='')
    source_row = models.CharField(max_length=100, blank=True, default='')
    content_hash = models.CharField(max_length=64, db_index=True)
    data = models.JSONField(default=dict, blank=True)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_questionnaire'
        ordering = ['-submitted_at']

    def __str__(self):
        return f"questionnaire#{self.pk} {self.submitted_at:%Y-%m-%d %H:%M}"


PRODREQUEST_STATUS_CHOICES = [
    ('new', 'New'),
    ('review', 'Review'),
    ('blocked', 'Blocked'),
    ('ready', 'Ready'),
    ('linked', 'Linked'),
    ('closed', 'Closed'),
]


class ProdRequest(models.Model):
    """
    PWG/DSC production request — upstream of ProdTask.

    Captures the request spreadsheet fields, system fields for intake
    idempotency and traceability, and production-team triage state.
    Use flags and requestor are duplicated onto ProdTask at task
    creation; both rows are mutable independently after that.
    """
    questionnaire = models.ForeignKey(
        Questionnaire, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='prod_requests',
        help_text="Originating Google Form response mirror, if linked",
    )

    # Requester-facing fields (request spreadsheet)
    requestor = models.CharField(max_length=100, blank=True, default='',
                                 help_text="PWG or DSC making the request")
    simu_path = models.CharField(max_length=500, blank=True, default='',
                                 help_text="Declared simulation/EVGEN input location")
    gen_config = models.TextField(blank=True, default='',
                                  help_text="Generator configuration text from the request")
    nevents = models.BigIntegerField(null=True, blank=True,
                                     help_text="Requested event count")
    background = models.CharField(max_length=200, blank=True, default='',
                                  help_text="Requested background condition")
    description = models.TextField(blank=True, default='')
    priority = models.IntegerField(null=True, blank=True,
                                   help_text="Requester or production priority")

    # Use flags
    pre_tdr_use = models.BooleanField(default=False)
    early_science_use = models.BooleanField(default=False)
    other_use = models.BooleanField(default=False)
    new_request = models.BooleanField(default=False)

    # System / traceability
    status = models.CharField(max_length=20, default='new')
    source_url = models.CharField(max_length=500, blank=True, default='',
                                  help_text="Source spreadsheet or form URL")
    source_row = models.CharField(max_length=100, blank=True, default='',
                                  help_text="Source row identifier for idempotent import")

    # Production-team triage
    input_status = models.CharField(max_length=100, blank=True, default='',
                                    help_text="Whether declared inputs are located/registered/usable")
    rucio_source = models.CharField(max_length=300, blank=True, default='',
                                    help_text="Rucio DID or container for registered input")
    validation_status = models.CharField(max_length=100, blank=True, default='',
                                         help_text="Validation summary")

    data = models.JSONField(default=dict, blank=True,
                            help_text="Flexible extension fields (no migration to add new keys)")
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_prod_request'
        ordering = ['-updated_at']

    def __str__(self):
        return f"req#{self.pk} {self.requestor or '(unspecified)'}"


PRODTASK_STATUS_CHOICES = [
    ('draft', 'Draft'),
    ('ready', 'Ready'),
    ('submitted', 'Submitted'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
]


class ProdTask(models.Model):
    """
    A production task: Dataset + ProdConfig + submission-specific params.
    Fully defines a production submission from which Condor and PanDA
    commands can be generated.
    """
    name = models.CharField(max_length=255, unique=True,
                            help_text="Task name (auto-derived from dataset or manual)")
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, default='draft')

    # Core composition
    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT, related_name='prod_tasks')
    prod_config = models.ForeignKey(ProdConfig, on_delete=models.PROTECT, related_name='prod_tasks')

    # Campaign / Request linkage
    campaign = models.ForeignKey(Campaign, null=True, blank=True,
                                 on_delete=models.PROTECT, related_name='prod_tasks',
                                 help_text="Campaign this task belongs to")
    request = models.ForeignKey(ProdRequest, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name='prod_tasks',
                                help_text="Originating PWG/DSC request, if any")

    # Catalog row fields (seeded from request at creation; mutable thereafter)
    requestor = models.CharField(max_length=100, blank=True, default='')
    priority = models.IntegerField(null=True, blank=True)
    pre_tdr_use = models.BooleanField(default=False)
    early_science_use = models.BooleanField(default=False)
    other_use = models.BooleanField(default=False)
    new_request = models.BooleanField(default=False)

    # Task-specific submission params
    csv_file = models.CharField(max_length=500, blank=True, default='',
                                help_text="CSV file path in simulation_campaign_datasets")
    overrides = models.JSONField(null=True, blank=True,
                                 help_text="Per-task overrides of ProdConfig fields (JSON)")

    # Generated commands (cached on save)
    condor_command = models.TextField(blank=True, default='',
                                      help_text="Generated Condor submission command")
    panda_command = models.TextField(blank=True, default='',
                                     help_text="Generated PanDA submission command")

    # Submission tracking
    panda_task_id = models.BigIntegerField(null=True, blank=True,
                                            help_text="PanDA task ID after submission")
    condor_cluster_id = models.CharField(max_length=100, blank=True, default='',
                                          help_text="Condor cluster ID after submission")

    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_prod_task'
        ordering = ['-updated_at']

    def __str__(self):
        return self.name

    @cached_property
    def composed_name(self):
        """The task's canonical identity: its dataset's composed tag name
        (group.EIC.<detector_version>.<detector>.p.e.s.r[.k][.sample]). This is
        what every task URL and API lookup keys on; the stored ``name`` may be a
        legacy csv_import slash path, kept resolvable but never the identity.
        ``build_dataset_name`` is the single authority, so this can never drift
        from the rendered name. Falls back to ``name`` only for the
        schema-impossible datasetless task."""
        return self.dataset.composed_name if self.dataset_id else self.name

    def _dids_from_overrides(self, list_key, single_key=None):
        """Resolve a list of DIDs from overrides JSON.

        Reads ``overrides[list_key]`` if it is a non-empty list; else, if
        ``single_key`` is provided and present, wraps that single DID; else
        returns an empty list. JSON-only, no schema column — per
        PCS_DATASET_REQUEST_WORKFLOW.md interim model.
        """
        ov = self.overrides or {}
        dids = ov.get(list_key)
        if isinstance(dids, list) and dids:
            return [d for d in dids if d]
        if single_key:
            single = ov.get(single_key)
            if single:
                return [single]
        return []

    def _resolve_datasets(self, dids):
        """Resolve a list of DIDs to Dataset objects, preserving order.
        DIDs not found are silently dropped.
        """
        if not dids:
            return []
        by_did = {d.did: d for d in Dataset.objects.filter(did__in=dids)}
        return [by_did[d] for d in dids if d in by_did]

    @property
    def input_datasets(self):
        """List of input Datasets from overrides['input_dataset_dids'] or
        the singular 'input_dataset_did' (back-compat shortcut for the
        single-input EVGEN case)."""
        return self._resolve_datasets(
            self._dids_from_overrides('input_dataset_dids', 'input_dataset_did')
        )

    @property
    def output_dataset_overrides(self):
        """Datasets named by ``overrides['output_dataset_dids']`` only.

        This is not the canonical task output. The canonical output/requested
        dataset is the ``dataset`` FK until that field is renamed by migration.
        """
        dids = self._dids_from_overrides('output_dataset_dids')
        return self._resolve_datasets(dids)

    @property
    def intermediate_datasets(self):
        """List of intermediate Datasets from overrides['intermediate_dataset_dids']."""
        return self._resolve_datasets(
            self._dids_from_overrides('intermediate_dataset_dids')
        )

    @property
    def input_dataset(self):
        """Single helper: first of ``input_datasets`` (or None)."""
        inputs = self.input_datasets
        return inputs[0] if inputs else None

    @property
    def output_dataset(self):
        """Compatibility alias for the canonical output/requested dataset.

        Do not read ``overrides['output_dataset_dids']`` here; that override
        list is exposed separately as ``output_dataset_overrides``.
        """
        return self.dataset if self.dataset_id else None

    @property
    def outputs(self):
        """Produced Rucio datasets — one entry per dataset, never aggregated,
        lifecycle-neutral. The single home for the produced-output ↔ task
        association, stored in ``overrides['outputs']``; each entry is
        ``{did, stage, version, filters, rses:[{rse,files,total,complete}],
        file_count, bytes, complete, checked_at}``. Supersedes the old
        ``csv_import.output`` rollup and the ``past_output`` block — see
        EPICPROD_DATA_LINEAGE.md."""
        out = (self.overrides or {}).get('outputs')
        return out if isinstance(out, list) else []

    @property
    def has_output(self):
        """True if any produced Rucio dataset is recorded."""
        return bool(self.outputs)

    @property
    def output_stages(self):
        """Distinct production stages present in outputs, e.g. ['FULL', 'RECO']."""
        return sorted({o.get('stage') for o in self.outputs if o.get('stage')})

    @property
    def output_incomplete(self):
        """True if any produced dataset is not fully replicated at every RSE."""
        return any(not o.get('complete', True) for o in self.outputs)

    @property
    def inputs(self):
        """Resolved Rucio EVGEN input datasets — the matched entries written by
        the EVGEN assimilation onto the bound Dataset's
        ``metadata['rucio']['matched']`` (each ``{did, stage, filters, rses,
        file_count, bytes, complete, checked_at}``). The input-side analog of
        ``outputs``; see EPICPROD_EVGEN_INPUTS.md."""
        md = (self.dataset.metadata or {}) if self.dataset_id else {}
        matched = (md.get('rucio') or {}).get('matched')
        return matched if isinstance(matched, list) else []

    @property
    def has_input(self):
        """True if the EVGEN input request resolved to one or more Rucio datasets."""
        return bool(self.inputs)

    @property
    def input_incomplete(self):
        """True if any resolved Rucio input is not fully replicated at every RSE."""
        return any(not i.get('complete', True) for i in self.inputs)

    @property
    def input_source_kind(self):
        """Source kind of the external input. Linked Dataset wins; csv_file fallback."""
        ds = self.input_dataset
        if ds:
            return ds.source_kind
        return 'csv_manifest' if self.csv_file else ''

    @property
    def input_source_location(self):
        """Source location of the external input. Linked Dataset wins; csv_file fallback."""
        ds = self.input_dataset
        if ds:
            return ds.source_location
        return self.csv_file or ''

    @property
    def input_source_stage(self):
        """Stage of the external input. Linked Dataset wins; csv_file → 'evgen'."""
        ds = self.input_dataset
        if ds:
            return ds.stage
        return 'evgen' if self.csv_file else ''

    def get_effective_config(self):
        """Return ProdConfig field values with per-task overrides applied."""
        config = self.prod_config
        overrides = self.overrides or {}
        result = {}
        for field in config._meta.get_fields():
            if not hasattr(field, 'attname'):
                continue
            name = field.name
            if name in ('id', 'created_at', 'updated_at'):
                continue
            result[name] = overrides.get(name, getattr(config, name))
        # Merge the data dicts specially (override keys, not replace entire dict)
        base_data = config.data or {}
        override_data = overrides.get('data', {})
        if isinstance(override_data, dict):
            result['data'] = {**base_data, **override_data}
        return result

    def generate_commands(self):
        """Generate and cache both Condor and PanDA commands."""
        from .commands import build_condor_command, build_panda_command
        self.condor_command = build_condor_command(self)
        self.panda_command = build_panda_command(self)


class PandaTasks(models.Model):
    """PanDA/JEDI task associations for one PCS production task.

    The plural class name is intentional: this is the association/history record
    for PanDA task attempts attached to a ProdTask, not a one-to-one peer model.
    """
    prod_task = models.ForeignKey(
        ProdTask, on_delete=models.CASCADE, related_name='panda_tasks',
    )
    try_number = models.PositiveIntegerField(default=1)
    jedi_task_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    task_name = models.CharField(max_length=300, unique=True)
    out_ds = models.CharField(max_length=300, blank=True, default='')
    log_ds = models.CharField(max_length=300, blank=True, default='')
    site = models.CharField(max_length=100, blank=True, default='')
    status_snapshot = models.CharField(max_length=50, blank=True, default='')
    association_source = models.CharField(max_length=50, blank=True, default='')
    match_reason = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pcs_panda_tasks'
        ordering = ['prod_task', 'try_number']
        constraints = [
            models.UniqueConstraint(
                fields=['prod_task', 'try_number'],
                name='pcs_panda_tasks_unique_try',
            ),
            models.UniqueConstraint(
                fields=['jedi_task_id'],
                condition=models.Q(jedi_task_id__isnull=False),
                name='pcs_panda_tasks_unique_jedi_task',
            ),
        ]

    def __str__(self):
        return f"{self.prod_task.composed_name} try{self.try_number}"


def _allocate_simple_tag(state_key):
    """Atomically allocate the next tag number using PersistentState."""
    from monitor_app.models import PersistentState
    with transaction.atomic():
        obj, _ = PersistentState.objects.select_for_update().get_or_create(
            id=1, defaults={'state_data': {}}
        )
        current = obj.state_data.get(state_key, 1)
        obj.state_data[state_key] = current + 1
        obj.save()
        return current
