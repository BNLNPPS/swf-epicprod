# PCS Commissioning Relaxations

During alpha commissioning, PCS deliberately relaxes its tag-locking and
submission guards so operators can shape the campaign-to-tag mapping freely —
create, edit, re-associate, and submit without a one-way freeze blocking the
corrections still being made. These relaxations are temporary. This note records
each one and exactly how to restore it, so tightening as the system is
commissioned does not require code archaeology.

The lock *mechanism* is preserved, only its *requirement* is lifted: the
deliberate tag-lock action (`POST /pcs/api/<type>-tags/<n>/lock/`) and the
immutability guard on a locked tag (`_TagViewSetMixin.partial_update`,
`pcs/api_views.py`) are unchanged. An operator can still lock any tag, and a
locked tag still cannot be edited. Tightening means re-enabling the requirements
below; the enforcement machinery already exists.

Submission never had an explicit "tags must be locked" check. The guarantee was
implicit: composition (relaxation 2) required locked tags, so every task's
dataset carried locked tags by the time it could be submitted. Relaxing
composition therefore already removed that implicit submission-provenance;
relaxation 4 additionally lifts the task-level `ready` freeze.

## Relaxations and how to restore them

| # | Relaxed | Where (symbol) | Restore by | Status |
|---|---------|----------------|-----------|--------|
| 1 | Auto-derived tags are created `draft`, not `locked`; a matched draft is reused as-is rather than locked in place. | `find_or_create_physics_tag`, `find_or_create_background_tag`, `find_or_create_evgen_tag` — `pcs/services.py` | Create with `status='locked'`; restore the "lock a matched draft in place" branch (the former `reuse-locked` / `lock-in-place` actions). | Active |
| 2 | Datasets accept `draft` tags — the "tag must be locked before use" requirement is dropped from composition. | `Dataset.clean` (`pcs/models.py`); `DatasetViewSet.create` (`pcs/api_views.py`); `dataset_intake` (`pcs/services.py`); `DatasetForm` tag fields (`pcs/forms.py`); `_ensure_csvimport_anchors` (`pcs/services.py`) | Re-add the `status != 'locked'` → reject check in `Dataset.clean`, `DatasetViewSet.create`, and `dataset_intake`; filter the `DatasetForm` tag querysets to `status='locked'`; have `_ensure_csvimport_anchors` require a locked anchor again. | Active |
| 3 | All existing PCS tags were set to `draft` (one-time data change). | migration `0012_unlock_all_tags_draft` (`pcs/migrations/`) | Not a global revert — lock each tag deliberately, via the lock action, as it is finalised. | Active |
| 4 | A `draft` task can be submitted directly; the `status='ready'` freeze is dropped, and `prodtask_readiness_problems` is surfaced as a non-blocking `warnings` field on the submit response rather than gating. | `prodtask_submit_request` and `prodtask_record_submission` ready-gates (`pcs/services.py`); the `submit` action warning surface (`pcs/api_views.py`) | Restore the `status != 'ready'` raise in both `prodtask_submit_request` and `prodtask_record_submission`; drop the warnings surface. The `draft→ready` readiness gate (`prodtask_set_status`) is unchanged and still blocks the deliberate lock-to-ready path. | Active |

## What is not relaxed

- The tag lock action and the locked-tag edit guard (above) — the means of tightening.
- The readiness *checks* themselves still run (`prodtask_readiness_problems`: output configured, physics tag beam matches the sample). They describe submission correctness, not provenance, so under relaxation 4 they are surfaced as a warning rather than blocking; the checks are not removed.

## Related

- [PCS.md](PCS.md) — tag lifecycle (draft → locked).
- [PCS_BACKGROUND_TAG.md](PCS_BACKGROUND_TAG.md), [EPICPROD_QUESTIONNAIRE.md](EPICPROD_QUESTIONNAIRE.md) — the entities these guards apply to.
