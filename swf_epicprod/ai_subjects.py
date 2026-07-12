"""epicprod assessment subject types, registered on the platform mechanism.

The AI-content mechanism (registration, retrieval, corun storage) is
platform and lives in ``monitor_app.mcp.ai_content``; what an assessment
can be *about* is domain vocabulary. This module defines the epicprod
subject resolvers and registers them — the same mechanism/policy split
the action registry uses (ARCHITECTURE_MAP.md § Adjudications).

A resolver takes ``(subject_key, data)`` and returns the resolved
subject dict: target_obj, target_json_field (the JSON pointer field on
the subject object), canonical subject_key, subject_label, subject_url.
"""

from urllib.parse import urlencode, urlparse

from django.urls import reverse

from monitor_app.mcp.ai_content import register_subject_type
from monitor_app.mcp.common import _monitor_url


def _url(name, *args, query=None):
    path = reverse(name, args=args)
    if query:
        path = f'{path}?{urlencode(query)}'
    # reverse() carries the deployment prefix when FORCE_SCRIPT_NAME is
    # set, and _monitor_url's base ends with the same prefix — strip the
    # duplicate so subject URLs don't come out /swf-monitor/swf-monitor/.
    base_path = urlparse(_monitor_url('')).path.rstrip('/')
    if base_path and path.startswith(f'{base_path}/'):
        path = path[len(base_path):]
    return _monitor_url(path)


def _resolve_prod_task(subject_key, data):
    from pcs.models import ProdTask
    from pcs import services

    qs = ProdTask.objects.select_related('dataset', 'prod_config')
    task = services.resolve_prodtask(subject_key, queryset=qs)
    name = task.composed_name
    return {
        'target_obj': task,
        'target_json_field': 'overrides',
        'subject_key': name,
        'subject_label': name,
        'subject_url': _url(
            'pcs:prod_task_compose',
            query={'tab': 'tasks', 'selected': name},
        ),
    }


def _resolve_panda_tasks(subject_key, data):
    from pcs.models import PandaTasks

    key = str(subject_key).strip()
    qs = PandaTasks.objects.select_related('prod_task', 'prod_task__dataset')
    if key.isdigit():
        row = qs.filter(jedi_task_id=int(key)).first()
    else:
        row = qs.filter(task_name=key).first()
    if row is None:
        raise PandaTasks.DoesNotExist(f'No PandaTasks row matches {subject_key!r}')
    display_key = row.jedi_task_id or row.task_name
    subject_url = ''
    if row.jedi_task_id:
        subject_url = _url('monitor_app:panda_task_detail', row.jedi_task_id)
    return {
        'target_obj': row,
        'target_json_field': 'metadata',
        'subject_key': str(display_key),
        'subject_label': row.task_name,
        'subject_url': subject_url,
    }


def _resolve_epicprod_job(subject_key, data):
    from monitor_app.models import EpicProdJob

    pandaid = int(str(subject_key).strip())
    row = EpicProdJob.objects.get(pandaid=pandaid)
    return {
        'target_obj': row,
        'target_json_field': 'data',
        'subject_key': str(row.pandaid),
        'subject_label': f'PanDA job {row.pandaid}',
        'subject_url': _url('monitor_app:panda_job_detail', row.pandaid),
    }


def _resolve_panda_queue(subject_key, data):
    from monitor_app.models import PandaQueue

    queue_name = str(subject_key).strip()
    defaults = {
        'site': str((data or {}).get('site') or queue_name),
        'status': str((data or {}).get('status') or 'active'),
        'queue_type': str((data or {}).get('queue_type') or ''),
        'config_data': (data or {}).get('config_data') or {},
    }
    row, _ = PandaQueue.objects.get_or_create(
        queue_name=queue_name,
        defaults=defaults,
    )
    return {
        'target_obj': row,
        'target_json_field': 'metadata',
        'subject_key': row.queue_name,
        'subject_label': row.queue_name,
        'subject_url': _url('monitor_app:epic_queue_detail', row.queue_name),
    }


def _resolve_campaign(subject_key, data):
    from pcs.models import Campaign

    campaign = Campaign.objects.get(name=str(subject_key).strip())
    return {
        'target_obj': campaign,
        'target_json_field': 'data',
        'subject_key': campaign.name,
        'subject_label': f'Campaign {campaign.name}',
        'subject_url': _url(
            'pcs:pcs_catalog',
            query={'lifecycle': campaign.lifecycle},
        ),
    }


register_subject_type(
    'campaign_task', _resolve_prod_task,
    aliases=('ctask', 'prod_task', 'prodtask', 'pcs.prod_task'))
register_subject_type(
    'panda_task', _resolve_panda_tasks,
    aliases=('ptask', 'jedi_task', 'jedi', 'panda_tasks', 'pcs.panda_tasks'))
register_subject_type(
    'panda_job', _resolve_epicprod_job,
    aliases=('job', 'epicprod_job', 'monitor.epicprod_job'))
register_subject_type(
    'panda_queue', _resolve_panda_queue,
    aliases=('queue', 'site', 'monitor.panda_queue'))
register_subject_type(
    'campaign', _resolve_campaign,
    aliases=('pcs.campaign',))
