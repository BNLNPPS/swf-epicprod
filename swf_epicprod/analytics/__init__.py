"""Campaign analytics library — the deterministic computation layer
beneath the campaign assessments (docs/EPICPROD_ASSESSMENTS_V1.md).

Every number an assessment states originates here. Members compute data
blocks from state the system already records; the rollup composes them
and computes the mechanical verdict floor. No member touches a
credential.
"""

from .rollup import campaign_status, producing_campaigns, resolve_target_campaigns

__all__ = ['campaign_status', 'producing_campaigns', 'resolve_target_campaigns']
