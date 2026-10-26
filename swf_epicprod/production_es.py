"""Bounded, whole production tasks using the proven fine-grained ES runner."""
import re
import hashlib
import shlex


def definition(settings):
    """Validate the declared whole-task budget before building any submission."""
    cfg = dict(settings)
    for key in ('events', 'input_events', 'events_per_job', 'unit_events', 'close_s', 'deadline_s',
                'margin_s', 'memory_mb_per_core'):
        value = cfg.get(key)
        if type(value) is not int or value <= 0:
            raise ValueError('production ES ' + key + ' must be a positive integer')
    if cfg['events'] % cfg['events_per_job']:
        raise ValueError('production ES events must be divisible by events_per_job')
    if cfg['events'] != cfg['input_events']:
        raise ValueError('production ES generation plan and whole-task budget disagree')
    if cfg['margin_s'] >= cfg['deadline_s']:
        raise ValueError('production ES drain margin must precede its deadline')
    if not re.fullmatch(r'[^\s,]+:[^\s,]+', str(cfg.get('input_dataset') or '')):
        raise ValueError('production ES needs its registered PanDA input dataset')
    row = str(cfg.get('row') or '').split(',')
    if (len(row) != 4 or row[1] != 'steer' or row[3] != '0000'
            or not row[0] or any(c.isspace() for c in row[0])):
        raise ValueError('production ES needs one complete, zero-offset particle-gun steering row')
    if int(row[2]) != cfg['events']:
        raise ValueError('production ES row and whole-task event budgets disagree')
    slots = str(cfg.get('slots', 'auto'))
    if slots != 'auto' and (not slots.isdigit() or int(slots) <= 0):
        raise ValueError('production ES slots must be auto or a positive integer')
    cfg['slots'] = slots
    if 'pilot_cores' in cfg and (type(cfg['pilot_cores']) is not int or cfg['pilot_cores'] <= 0):
        raise ValueError('production ES pilot_cores must be a positive integer')
    if 'batch_channel' in cfg and type(cfg['batch_channel']) is not bool:
        raise ValueError('production ES batch_channel must be a boolean')
    if 'min_unit_events' in cfg and (type(cfg['min_unit_events']) is not int or not 1 <= cfg['min_unit_events'] <= cfg['unit_events']):
        raise ValueError('production ES min_unit_events must fit its unit budget')
    if 'parallel_start' in cfg and type(cfg['parallel_start']) is not bool:
        raise ValueError('production ES parallel_start must be a boolean')
    if 'fast_merge' in cfg and type(cfg['fast_merge']) is not bool:
        raise ValueError('production ES fast_merge must be a boolean')
    if 'resident_sim' in cfg and type(cfg['resident_sim']) is not bool:
        raise ValueError('production ES resident_sim must be a boolean')
    if 'resident_unit' in cfg and type(cfg['resident_unit']) is not bool:
        raise ValueError('production ES resident_unit must be a boolean')
    if cfg.get('resident_unit') and not cfg.get('resident_sim'):
        raise ValueError('production ES resident_unit needs resident_sim')
    if 'close_max_files' in cfg and (type(cfg['close_max_files']) is not int or cfg['close_max_files'] <= 0):
        raise ValueError('production ES close_max_files must be a positive integer')
    if 'reco_pool' in cfg and (type(cfg['reco_pool']) is not int or cfg['reco_pool'] <= 0):
        raise ValueError('production ES reco_pool must be a positive integer')
    if 'reco_async' in cfg and type(cfg['reco_async']) is not bool:
        raise ValueError('production ES reco_async must be a boolean')
    if cfg.get('reco_async') and not cfg.get('resident_unit'):
        raise ValueError('production ES reco_async needs resident_unit')
    if 'report_barrier' in cfg and type(cfg['report_barrier']) is not bool:
        raise ValueError('production ES report_barrier must be a boolean')
    return cfg


def apply(spec, settings):
    """Keep the ordinary PCS producer, provenance, outputs and recording path."""
    cfg = definition(settings)
    if spec.get('trial'):
        raise ValueError('a production ES sample is a whole production task, not a trial')
    outputs = spec.get('outputs') or []
    if len(outputs) != 1 or outputs[0].get('level') != 'RECO':
        raise ValueError('production ES currently requires one RECO output dataset')
    dataset = outputs[0]['dataset']
    if not dataset.startswith('/RECO/'):
        raise ValueError('production ES output must use the production RECO namespace')
    env = spec['env']
    if env.get('EVGEN_GENERATOR') != 'particle_gun' or env.get('EVGEN_INTERNAL') != 'true':
        raise ValueError('additional production statistics require an internal particle gun')
    if str(env.get('COPYFULL', '')).lower() == 'true':
        raise ValueError('production ES FULL retention is not supported')
    env['EPICPROD_SWITCH_ES_GUN_EVENTS'] = '1'
    if cfg.get('resident_sim'):
        env['EPICPROD_SWITCH_RESIDENT_SIM'] = '1'
    prefix = {
        'ES_WORKER_PROXY': '1',
        'ES_PAYLOAD_IMAGE': spec['containerImage'], 'ES_SLOTS': cfg['slots'],
        'ES_EVENTS_PER_UNIT': str(cfg['unit_events']), 'ES_CLOSE_S': str(cfg['close_s']),
        'ES_DEADLINE_S': str(cfg['deadline_s']), 'ES_MARGIN_S': str(cfg['margin_s']),
        'ES_OUTPUT_DATASET': dataset,
    }
    if cfg.get('batch_channel'):
        prefix['EPICPROD_SWITCH_ES_BATCH_CHANNEL'] = '1'
    if cfg.get('min_unit_events'):
        prefix['ES_MIN_UNIT_EVENTS'] = str(cfg['min_unit_events'])
    if cfg.get('parallel_start'):
        prefix['EPICPROD_SWITCH_ES_COHORT_START'] = '1'
    if cfg.get('fast_merge'):
        prefix['EPICPROD_SWITCH_ES_FAST_MERGE'] = '1'
    if cfg.get('resident_unit'):
        prefix['EPICPROD_SWITCH_ES_RESIDENT_UNIT'] = '1'
    if cfg.get('close_max_files'):
        prefix['ES_CLOSE_MAX_FILES'] = str(cfg['close_max_files'])
    if cfg.get('reco_pool'):
        prefix['EPICPROD_SWITCH_ES_RECO_POOL'] = str(cfg['reco_pool'])
    if cfg.get('reco_async'):
        prefix['EPICPROD_SWITCH_ES_RECO_ASYNC'] = '1'
    if cfg.get('report_barrier'):
        prefix['EPICPROD_SWITCH_ES_REPORT_BARRIER'] = '1'
    command = ' '.join(k + '=' + shlex.quote(v) for k, v in prefix.items())
    command += (' python3 evgen_job_dispatcher.py es-production '
                + shlex.quote(spec['csvBase']) + ' ' + shlex.quote(spec['outDS'])
                + ' $PILOT_EVENTRANGECHANNEL --accessmode=direct')
    spec.update(exec=command, fineGrainedProc=True, directInput=True,
                inputDataset=cfg['input_dataset'], nFilesPerJob=1,
                nEventsPerWorker=cfg['unit_events'],
                nEventsPerInputFile=cfg['input_events'], nEventsPerJob=cfg['events_per_job'],
                nJobs=cfg['events'] // cfg['events_per_job'], nCore=cfg.get('pilot_cores', 1),
                walltimeHours=cfg['deadline_s'] / 3600,
                memory=cfg['memory_mb_per_core'], maxAttempt=10, maxFailure=3,
                noLog=True, workerProxy=True, event_service=dict(cfg))
    return spec


def gun_seed(stem, offset):
    """The payload's positive 31-bit seed for a gun range's absolute start."""
    hashed = int(hashlib.sha256(stem.encode()).hexdigest()[:7], 16)
    return (hashed * 10000 + offset + 1) % 2147483647 or 1
