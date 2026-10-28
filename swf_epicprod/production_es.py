"""Bounded, whole production tasks using the proven fine-grained ES runner."""
import re
import hashlib
import shlex

# Events per range of a fine-grained task above one need JEDI to cut such
# ranges: upstream's job generator gives a fine-grained task single-event
# ranges, and our server's local patch adds the task parameter
# nEventsPerFineGrainedRange (split rule FR; distcomp-services
# patches/fine-grained-range-size), which the submitter sets from
# range_events (swf-monitor scripts/evgen_panda_submit.py). The payload, the
# close-out and the batch pilot take ranges of any size
# (NODE_EVENT_DISPATCHER.md); the stock pilot counts a range as one event, so
# ranges above one need the batch channel. On since 2026-10-27: Perlmutter
# task 41444 (3,200 ranges of ten events) was credited in full, 32,000 events.
JEDI_FINE_GRAINED_RANGES = True


def definition(settings):
    """Validate the declared whole-task budget before building any submission.

    Two inputs: an internal particle gun (the default: one steering row, its
    generation plan the input), or a registered EVGEN sample (``input``
    ``evgen``, swf_epicprod/evgen_es.py): the files its manifest covers,
    staged at BNL, one job per file over ``events_per_file`` events each, the
    task capped at ``events``."""
    cfg = dict(settings)
    evgen = cfg.get('input') == 'evgen'
    keys = ('events', 'unit_events', 'close_s', 'deadline_s', 'margin_s', 'memory_mb_per_core')
    keys += ('events_per_file', 'files') if evgen else ('input_events', 'events_per_job')
    for key in keys:
        value = cfg.get(key)
        if type(value) is not int or value <= 0:
            raise ValueError('production ES ' + key + ' must be a positive integer')
    if cfg['margin_s'] >= cfg['deadline_s']:
        raise ValueError('production ES drain margin must precede its deadline')
    if not re.fullmatch(r'[^\s,]+:[^\s,]+', str(cfg.get('input_dataset') or '')):
        raise ValueError('production ES needs its registered PanDA input dataset')
    if evgen:
        # A job takes one file; JEDI stops at the task's events, so the
        # last file is taken only as far as the manifest's total.
        cfg['events_per_job'] = cfg['events_per_file']
        if cfg['events'] > cfg['files'] * cfg['events_per_file']:
            raise ValueError('production ES events exceed the staged files')
        if not cfg.get('resident_sim'):
            raise ValueError('production ES over EVGEN input needs the resident simulation')
    else:
        if cfg['events'] % cfg['events_per_job']:
            raise ValueError('production ES events must be divisible by events_per_job')
        if cfg['events'] != cfg['input_events']:
            raise ValueError('production ES generation plan and whole-task budget disagree')
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
    if 'range_events' in cfg:
        k = cfg['range_events']
        if type(k) is not int or k <= 0 or cfg['unit_events'] % k:
            raise ValueError('production ES range_events must be a positive divisor of unit_events')
        if cfg['events'] % k:
            # Every range full: the batch pilot rebuilds a range it never
            # received from its first event and the range size (es_batch.py,
            # Prefetch._reconcile).
            raise ValueError('production ES range_events must divide the events')
        if evgen and cfg['events_per_file'] % k:
            raise ValueError('production ES range_events must divide each file\'s events')
        if k > 1 and not JEDI_FINE_GRAINED_RANGES:
            raise ValueError('production ES range_events above 1 needs JEDI ranges of several events '
                             'for fine-grained tasks')
        if k > 1 and not cfg.get('batch_channel'):
            raise ValueError('production ES range_events above 1 needs the batch channel: '
                             'the stock pilot counts a range as one event')
    if 'job_log' in cfg and type(cfg['job_log']) is not bool:
        raise ValueError('production ES job_log must be a boolean')
    if cfg.get('job_log') and not cfg.get('batch_channel'):
        # The batch pilot writes the ES log beside the renamed work directory
        # and trims it; the stock pilot makes none and fails the stage-out
        # (NERSC_PERLMUTTER.md, The batch pilot, The job's log).
        raise ValueError('production ES job_log needs the batch channel')
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
    if 'close_concurrency' in cfg and (type(cfg['close_concurrency']) is not int or cfg['close_concurrency'] <= 0):
        raise ValueError('production ES close_concurrency must be a positive integer')
    if 'reco_pool' in cfg and (type(cfg['reco_pool']) is not int or cfg['reco_pool'] <= 0):
        raise ValueError('production ES reco_pool must be a positive integer')
    if 'reco_async' in cfg and type(cfg['reco_async']) is not bool:
        raise ValueError('production ES reco_async must be a boolean')
    if cfg.get('reco_async') and not cfg.get('resident_unit'):
        raise ValueError('production ES reco_async needs resident_unit')
    if 'report_barrier' in cfg and type(cfg['report_barrier']) is not bool:
        raise ValueError('production ES report_barrier must be a boolean')
    for key in ('handoff_in_place', 'unit_local', 'input_names', 'local_scratch', 'nersc_input',
                'local_work', 'record_per_close'):
        if key in cfg and type(cfg[key]) is not bool:
            raise ValueError(f'production ES {key} must be a boolean')
    if cfg.get('input_names') and not evgen:
        raise ValueError('production ES input_names needs EVGEN input')
    if cfg.get('nersc_input') and not evgen:
        raise ValueError('production ES nersc_input needs EVGEN input')
    if 'input_files' in cfg and (not evgen or not isinstance(cfg['input_files'], list)
                                 or len(cfg['input_files']) != cfg['files']):
        raise ValueError('production ES input_files must name each of its EVGEN files')
    if 'file_events' in cfg:
        fe, k = cfg['file_events'], cfg.get('range_events', 1)
        if (not isinstance(fe, dict) or list(fe) != list(cfg.get('input_files') or [])
                or any(type(n) is not int or n <= 0 or n % k for n in fe.values())
                or sum(fe.values()) != cfg['events'] or max(fe.values()) != cfg['events_per_file']):
            raise ValueError('production ES file_events must give each input file its events, '
                             'whole ranges summing to the task\'s events, the largest its events_per_file')
    if 'notice' in cfg and type(cfg['notice']) is not bool:
        raise ValueError('production ES notice must be a boolean')
    if 'preempt_at_s' in cfg and (type(cfg['preempt_at_s']) is not int or cfg['preempt_at_s'] <= 0):
        raise ValueError('production ES preempt_at_s must be a positive integer')
    if 'preempt_notice_s' in cfg:
        if type(cfg['preempt_notice_s']) is not int or cfg['preempt_notice_s'] <= 0:
            raise ValueError('production ES preempt_notice_s must be a positive integer')
        if not cfg.get('preempt_at_s') or not cfg.get('notice') or cfg.get('preempt_pilot'):
            raise ValueError('production ES preempt_notice_s needs preempt_at_s and notice, not preempt_pilot')
    for key in ('preempt_pilot', 'preempt_first_job'):
        if key in cfg and type(cfg[key]) is not bool:
            raise ValueError(f'production ES {key} must be a boolean')
        if cfg.get(key) and not cfg.get('preempt_at_s'):
            raise ValueError(f'production ES {key} needs preempt_at_s')
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
    evgen = cfg.get('input') == 'evgen'
    if evgen:
        if env.get('EVGEN_INTERNAL') == 'true':
            raise ValueError('production ES over EVGEN input needs an external EVGEN sample')
    elif env.get('EVGEN_GENERATOR') != 'particle_gun' or env.get('EVGEN_INTERNAL') != 'true':
        raise ValueError('additional production statistics require an internal particle gun')
    if str(env.get('COPYFULL', '')).lower() == 'true':
        raise ValueError('production ES FULL retention is not supported')
    if evgen:
        # Each slot's resident simulation reads the job's staged file
        # (NODE_EVENT_DISPATCHER.md, Resident simulation with external EVGEN
        # input; tasks 41241, 41262).
        env['EPICPROD_SWITCH_RESIDENT_EVGEN'] = '1'
    else:
        env['EPICPROD_SWITCH_ES_GUN_EVENTS'] = '1'
    if cfg.get('resident_sim'):
        env['EPICPROD_SWITCH_RESIDENT_SIM'] = '1'
    # In the sandbox's environment file, as canary 41481 carried them: run.sh,
    # the resident units and the harness each read them there
    # (EPICPROD_PAYLOAD.md, Resident Event Service units).
    if cfg.get('handoff_in_place'):
        # A unit's RECO is written in its handoff directory: on Perlmutter's
        # scratch the handoff's move between directories was served one unit
        # at a time across the node from 28 October (job 6289946).
        env['EPICPROD_SWITCH_ES_HANDOFF_IN_PLACE'] = '1'
    if cfg.get('unit_local'):
        # No unit makes a PanDA token request, catalog query or log upload
        # of its own; the close registers and the job's log carries the units'.
        env['EPICPROD_SWITCH_ES_UNIT_LOCAL'] = '1'
    if cfg.get('input_names'):
        # Units and closes are named from the job's own EVGEN file, not the
        # manifest's first row (task 41479's job on run002 closed as run001).
        env['EPICPROD_SWITCH_ES_INPUT_NAMES'] = '1'
    if cfg.get('nersc_input'):
        # The job reads its EVGEN file from the copy staged ahead at NERSC
        # (swf_epicprod/nersc_stage.py), never pulling it over the WAN at its
        # start (Torre Wenaus, 2026-10-28); with direct access below.
        env['EPICPROD_SWITCH_ES_NERSC_INPUT'] = '1'
    if cfg.get('local_work'):
        # The job's own transient files in the node's memory, only the logs on
        # the shared file system (canary 41486: 31 events/s, the step after
        # reconstruction no longer served one unit at a time).
        env['EPICPROD_SWITCH_ES_LOCAL_WORK'] = '1'
    if cfg.get('record_per_close'):
        # The units' records leave with their close; the job record and the
        # end-of-job report carry only units no close shipped.
        env['EPICPROD_SWITCH_ES_RECORD_PER_CLOSE'] = '1'
    if cfg.get('local_scratch'):
        # The units' scratch in the node's /tmp, off the shared file system:
        # on Perlmutter the step after a resident unit's reconstruction, which
        # removes that scratch, was served one unit at a time across the node
        # (canary 41481).
        env['EPICPROD_SWITCH_ES_LOCAL_SCRATCH'] = '1'
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
        # A slot's dead resident is restarted; a slot whose restarts are
        # spent takes no more units (job 6254767: one dead resident failed
        # 960 units, 480,000 ranges).
        prefix['EPICPROD_SWITCH_ES_RESIDENT_RESTART'] = '1'
    if cfg.get('close_max_files'):
        prefix['ES_CLOSE_MAX_FILES'] = str(cfg['close_max_files'])
    if cfg.get('close_concurrency'):
        prefix['ES_CLOSE_CONCURRENCY'] = str(cfg['close_concurrency'])
    if cfg.get('reco_pool'):
        prefix['EPICPROD_SWITCH_ES_RECO_POOL'] = str(cfg['reco_pool'])
    if cfg.get('reco_async'):
        prefix['EPICPROD_SWITCH_ES_RECO_ASYNC'] = '1'
    if cfg.get('report_barrier'):
        prefix['EPICPROD_SWITCH_ES_REPORT_BARRIER'] = '1'
    if cfg.get('notice'):
        # A preemption's SIGTERM ends the job in its notice (es_harness.py).
        prefix['EPICPROD_SWITCH_ES_NOTICE'] = '1'
    if cfg.get('preempt_at_s'):
        # A controlled preemption (es_harness.py --preempt-at-s): a test of
        # the close-out of a lost node, never a production shape by itself.
        prefix['ES_PREEMPT_AT_S'] = str(cfg['preempt_at_s'])
        if cfg.get('preempt_notice_s'):
            prefix['ES_PREEMPT_NOTICE_S'] = str(cfg['preempt_notice_s'])
        if cfg.get('preempt_pilot'):
            prefix['ES_PREEMPT_PILOT'] = '1'
        if cfg.get('preempt_first_job'):
            prefix['ES_PREEMPT_FIRST_JOB'] = '1'
    command =' '.join(k + '=' + shlex.quote(v) for k, v in prefix.items())
    # The gun's input is its small generation plan, read in place. An EVGEN
    # file is staged by the pilot, so the payload simulates from the job's
    # own file (run.sh, EPICPROD_INPUT_LOCAL), never from the door path of
    # the manifest's first row.
    command += (' python3 evgen_job_dispatcher.py es-production '
                + shlex.quote(spec['csvBase']) + ' ' + shlex.quote(spec['outDS'])
                + ' $PILOT_EVENTRANGECHANNEL'
                + ('' if evgen and not cfg.get('nersc_input') else ' --accessmode=direct'))
    if evgen and cfg.get('file_events'):
        # Each file at its own events, the last read only as far as the
        # manifest, and the task at their sum: JEDI reads each file's count
        # from the listing (useRealNumEvents), which the DDM interface gives
        # the task's own counts (epicFileEvents; swf_epicprod/jedi/
        # EpicDDMClient.py). One count for every file (nEventsPerInputFile)
        # made JEDI drop a last file the total takes only part way.
        # nEventsPerJob at the task's total: JEDI cuts a file's events into
        # records of nEventsPerJob carried across files, so anything less than
        # the total cuts a file after the first into a short extra job; one
        # file a job comes from nFilesPerJob.
        shape = dict(useRealNumEvents=True, epicFileEvents=dict(cfg['file_events']),
                     nEventsPerJob=cfg['events'], nJobs=cfg['files'], nEvents=cfg['events'])
    elif evgen:
        shape = dict(nEventsPerInputFile=cfg['events_per_file'], nEventsPerJob=cfg['events_per_file'],
                     nJobs=cfg['files'], nEvents=cfg['events'])
    else:
        shape = dict(nEventsPerInputFile=cfg['input_events'], nEventsPerJob=cfg['events_per_job'],
                     nJobs=cfg['events'] // cfg['events_per_job'])
    spec.update(exec=command, fineGrainedProc=True, directInput=not evgen,
                inputDataset=cfg['input_dataset'], nFilesPerJob=1,
                nEventsPerWorker=cfg['unit_events'], nCore=cfg.get('pilot_cores', 1),
                walltimeHours=cfg['deadline_s'] / 3600,
                memory=cfg['memory_mb_per_core'], maxAttempt=10, maxFailure=3,
                noLog=not cfg.get('job_log'), workerProxy=True, event_service=dict(cfg), **shape)
    if cfg.get('range_events', 1) > 1:
        spec['fineGrainedRangeEvents'] = cfg['range_events']
    if evgen and cfg.get('input_files'):
        # The input read where it resides: the catalog's dataset, limited to
        # the files the manifest covers (JEDI's template ``files``).
        spec['inputFiles'] = list(cfg['input_files'])
    return spec


def gun_seed(stem, offset):
    """The payload's positive 31-bit seed for a gun range's absolute start."""
    hashed = int(hashlib.sha256(stem.encode()).hexdigest()[:7], 16)
    return (hashed * 10000 + offset + 1) % 2147483647 or 1
