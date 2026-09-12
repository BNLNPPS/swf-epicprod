"""Permanent PCS identities. PostgreSQL is the final enforcement boundary."""
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import connections, models, transaction
from django.utils import timezone


LIFECYCLES = [('active', 'Active'), ('retired', 'Retired'),
              ('invalid', 'Invalid'), ('superseded', 'Superseded')]
PERMANENT_TABLES = (
    'pcs_physics_tag', 'pcs_evgen_tag', 'pcs_simu_tag', 'pcs_reco_tag',
    'pcs_background_tag', 'pcs_physics_config',
)


class PermanentIdentityQuerySet(models.QuerySet):
    def active(self):
        return self.filter(lifecycle='active')

    def delete(self):
        raise ValidationError('Issued PCS identities are permanent. Retire them with a reason.')


class PermanentIdentity(models.Model):
    lifecycle = models.CharField(max_length=16, choices=LIFECYCLES, default='active')
    lifecycle_reason = models.TextField(blank=True, default='')
    lifecycle_by = models.CharField(max_length=100, blank=True, default='')
    lifecycle_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True, related_name='+')

    objects = PermanentIdentityQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ValidationError('Issued PCS identities are permanent. Retire them with a reason.')

    @property
    def identity_label(self):
        return getattr(self, 'tag_label', None) or self.label


@contextmanager
def identity_change(actor, reason, using='default'):
    """Attribute trigger-recorded changes, restoring nested transaction context."""
    connection = connections[using]
    with transaction.atomic(using=using):
        if connection.vendor != 'postgresql':
            yield
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('pcs.actor', true), current_setting('pcs.reason', true)")
            previous = cursor.fetchone()
            cursor.execute("SELECT set_config('pcs.actor', %s, true), set_config('pcs.reason', %s, true)",
                           [actor or '', reason or ''])
        try:
            yield
        finally:
            if not connection.needs_rollback:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('pcs.actor', %s, true), set_config('pcs.reason', %s, true)",
                                   [previous[0] or '', previous[1] or ''])


def set_lifecycle(instance, lifecycle, *, reason, changed_by, replacement=None):
    """Dispose of an identity without erasing it or redirecting its old URL."""
    if not isinstance(lifecycle, str) or not isinstance(reason, str):
        raise ValidationError('A valid lifecycle and text reason are required.')
    reason = reason.strip()
    if lifecycle not in dict(LIFECYCLES) or not reason or not changed_by:
        raise ValidationError('A valid lifecycle, reason and actor are required.')
    if (lifecycle == 'superseded') != (replacement is not None):
        raise ValidationError('Only supersession requires a replacement identity.')
    if replacement is not None and (
            type(instance) is not type(replacement) or instance.pk == replacement.pk):
        raise ValidationError('The replacement must be a different identity of the same type.')
    using = instance._state.db or 'default'
    with identity_change(changed_by, reason, using):
        current = type(instance).objects.using(using).select_for_update().get(pk=instance.pk)
        if replacement is not None:
            replacement = type(instance).objects.using(using).get(pk=replacement.pk)
            if replacement.lifecycle != 'active':
                raise ValidationError('The replacement must be active.')
        current.lifecycle = lifecycle
        current.lifecycle_reason = reason
        current.lifecycle_by = changed_by
        current.lifecycle_at = timezone.now()
        current.superseded_by = replacement
        current.save(update_fields=['lifecycle', 'lifecycle_reason', 'lifecycle_by',
                                    'lifecycle_at', 'superseded_by', 'updated_at'])
    return current
