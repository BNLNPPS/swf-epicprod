"""A task ended quietly (docs/EPICPROD_OPS.md, Quiet end).

A commissioning test or a stopped task is ended without a trace in the
production record: no job is touched, the production readers leave it out, and
PanDA is told to finish it only when JEDI will end it ``finished`` (at least
one input file finished; otherwise JEDI ends it ``failed``). The intent is recorded on the PCS task,
``ProdTask.overrides['quiet_end'] = {by, at, reason}``, before PanDA is told to
finish it; ``tools/quiet_end.py`` does both.

``quiet_task_ids()`` is the one test the readers use: the dashboard's Paused
and Completed tasks (swf-monitor comp_snapshot), the daily assessment's window
figures (analytics/members.py) and Prod tasks in flight (pcs views).
"""

MARKER = 'quiet_end'
CACHE_KEY = 'quiet_end:jedi_task_ids:v1'
CACHE_S = 60


def quiet_task_ids():
    """The JEDI task ids of quietly ended PCS tasks, every attempt of each."""
    from django.core.cache import cache
    ids = cache.get(CACHE_KEY)
    if ids is None:
        from pcs.models import PandaTasks
        ids = set(PandaTasks.objects.filter(prod_task__overrides__has_key=MARKER)
                  .exclude(jedi_task_id=None).values_list('jedi_task_id', flat=True))
        cache.set(CACHE_KEY, ids, CACHE_S)
    return ids


def invalidate():
    from django.core.cache import cache
    cache.delete(CACHE_KEY)


# The PanDA side: run under the PanDA client environment, as the close-out's
# calls are (es_closeout.finish_in_pclient), with its _api.

def finish_task(task_id):
    """PanDA's soft finish of one task: no job is killed (a task with no
    active job goes straight to its end, pandajedi TaskCommando), and the task
    ends finished."""
    from swf_epicprod.es_closeout import _api
    http, resp = _api('task/finish', {'task_id': int(task_id), 'soft': True})
    ok = bool(isinstance(resp, dict) and resp.get('success'))
    return {'task_id': int(task_id), 'ok': ok, 'http': http,
            'response': resp if isinstance(resp, dict) else str(resp)[:400]}


def _inside_pclient(payload_path):
    import json
    with open(payload_path) as f:
        payload = json.load(f)
    results = []
    for task_id in payload['tasks']:
        try:
            results.append(finish_task(task_id))
        except Exception as e:                                # noqa: BLE001
            results.append({'task_id': task_id, 'ok': False, 'error': f'{type(e).__name__}: {e}'})
    print(json.dumps({'results': results}))
    return 0


def finish_in_pclient(task_ids):
    """finish_task for each of ``task_ids`` under the PanDA client environment."""
    import json
    import os
    import subprocess
    import tempfile
    from swf_epicprod.es_closeout import AUTH_VO, PCLIENT_SETUP, PCLIENT_TIMEOUT_S
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory(prefix='quiet-end-') as tmp:
        payload = os.path.join(tmp, 'payload.json')
        with open(payload, 'w') as f:
            json.dump({'tasks': [int(t) for t in task_ids]}, f)
        script = (f'source {PCLIENT_SETUP} >/dev/null 2>&1\n'
                  f'export PANDA_AUTH_VO={AUTH_VO}\n'
                  f'PYTHONPATH={package_root}${{PYTHONPATH:+:$PYTHONPATH}} '
                  f'python3 {os.path.abspath(__file__)} --inside-pclient {payload}\n')
        p = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                           timeout=PCLIENT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    lines = [ln for ln in (p.stdout or '').splitlines() if ln.startswith('{')]
    if p.returncode != 0 or not lines:
        raise RuntimeError(f'PanDA client step failed (exit {p.returncode}): '
                           f'{(p.stderr or p.stdout or "").strip()[-600:]}')
    return json.loads(lines[-1])['results']


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--inside-pclient':
        sys.exit(_inside_pclient(sys.argv[2]))
    print('usage: quiet_end.py --inside-pclient <payload.json>', file=sys.stderr)
    sys.exit(2)
