"""Analysis services: register tags, freeze dataset cards and runs, build
the PanDA task specifications (docs/ANALYSIS.md).

The web tier holds no credential and submits nothing: as for production,
a specification built here is handed to the operations agent, whose doer
assembles the sandbox and submits under its own credentials.
"""
import hashlib

import yaml
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import cards
from .models import AnalysisJobOutput, AnalysisRun, AnalysisTag, DatasetCard

#: EIC-XRD-LOG: unregistered uploads under /work/eic3/EPIC/eic, the RSE
#: production writes its logs to (docs/ANALYSIS.md, Storage).
SCRATCH_RSE = 'EIC-XRD-LOG'
CVMFS_IMAGES = '/cvmfs/singularity.opensciencegrid.org/eicweb'


# --- tags ------------------------------------------------------------------

def image_for(card):
    """The container a card's eic_shell names, as the jobs are given it."""
    shell = card['eic_shell']
    if shell.get('image'):
        return str(shell['image'])
    return f"{CVMFS_IMAGES}/{shell['container']}:{shell['version']}"


def register_tag(repo, commit, card_path, card_text, created_by, description=''):
    """Register an analysis card read at ``commit`` as the next tag, or
    return the tag already registered for the same pin.

    ``card_text`` is the file as it is at that commit; the caller reads
    it (the management command fetches it from the repository)."""
    commit = cards.check_commit(commit)
    try:
        card = yaml.safe_load(card_text)
    except yaml.YAMLError as e:
        raise cards.CardError(f'{card_path} is not YAML: {e}')
    cards.check_analysis_card(card)
    image = image_for(card)
    existing = AnalysisTag.objects.filter(
        repo=repo, commit=commit, card_path=card_path, image=image).first()
    if existing:
        return existing, False
    tag = AnalysisTag(repo=repo, commit=commit, card_path=card_path, card=card,
                      card_text=card_text,
                      card_sha256=hashlib.sha256(card_text.encode()).hexdigest(),
                      image=image, description=description or str(card.get('description') or ''),
                      created_by=created_by)
    tag.save()
    return tag, True


# --- dataset cards ---------------------------------------------------------

def delivered_files(dataset, sample):
    """The RECO files PCS records as delivered for ``dataset``, in the
    shape cards.manifest_rows reads: one row per registered file, with
    the producer's own unit key and the attempt that wrote it."""
    from pcs.models import DeliveredOutput
    rows = []
    qs = (DeliveredOutput.objects
          .filter(prod_task__dataset=dataset, kind='RECO',
                  status__in=('delivered', 'diverted'))
          .select_related('panda_task'))
    for out in qs:
        did = out.registered_did if out.status == 'diverted' and out.registered_did else out.did
        if cards.sample_of(did) != sample:
            continue
        rows.append({
            'did': did,
            'unit': out.segment or '',
            'try': out.panda_task.try_number if out.panda_task_id else None,
            'events': out.events,
            'bytes': out.bytes,
        })
    return rows


@transaction.atomic
def freeze_dataset_card(dataset, sample, frozen_by, files=None, source='delivered'):
    """Freeze ``sample`` of ``dataset`` into a dataset card.

    The files are PCS's delivered outputs unless ``files`` is given (a
    Rucio listing, for samples produced before delivered outputs were
    recorded; ``source='rucio'``). A list equal to the latest revision's
    returns that revision rather than a copy; anything else is the next
    revision. Returns (card, created)."""
    if files is None:
        files = delivered_files(dataset, sample)
        source = 'delivered'
    rows = cards.manifest_rows(files)
    latest = (DatasetCard.objects.select_for_update()
              .filter(sample=sample).order_by('-revision').first())
    digest = cards.canonical_sha256(rows)
    if latest and latest.manifest_sha256 == digest:
        return latest, False
    now = timezone.now()
    revision = (latest.revision + 1) if latest else 1
    header = cards.dataset_card(sample, revision, rows, source, now.isoformat())
    card = DatasetCard.objects.create(
        dataset=dataset, sample=sample, revision=revision, source=source,
        file_count=header['file_count'], events=header['events'],
        manifest_sha256=header['manifest_sha256'], manifest=cards.manifest_blob(rows),
        header=header, frozen_at=now, frozen_by=frozen_by)
    return card, True


# --- runs ------------------------------------------------------------------

def effective_split(run):
    return {'files_per_job': int(run.files_per_job or run.tag.card['split']['files_per_job'])}


def build_run_card(run):
    """The run card of a draft, from its tag, inputs, exclusions,
    parameters and split. Pure over the records; nothing is saved."""
    inputs = list(run.inputs.all())
    params = cards.effective_parameters(run.tag.card.get('parameters'), run.parameters)
    cards.params_string(params)  # refuse unusable values before freezing
    return cards.run_card(
        run.tag.pin(),
        [{'sample': c.sample, 'revision': c.revision, 'manifest_sha256': c.manifest_sha256}
         for c in inputs],
        [e['did'] if isinstance(e, dict) else e for e in run.exclusions or []],
        params, effective_split(run))


def freeze_run(run):
    """Freeze a draft: write its run card and id. A run with the same id
    already frozen is the same computation, so it is returned instead
    (and the draft left as it is). Returns (run, frozen_now)."""
    if run.is_frozen:
        return run, False
    card = build_run_card(run)
    run_id = cards.run_card_id(card)
    same = AnalysisRun.objects.filter(run_card_id=run_id).first()
    if same:
        return same, False
    run.run_card = card
    run.run_card_id = run_id
    run.campaign = cards.campaign_of(card['inputs'][0]['sample'])
    run.status = 'frozen'
    try:
        with transaction.atomic():
            run.save()
    except IntegrityError:
        return AnalysisRun.objects.get(run_card_id=run_id), False
    return run, True


def run_jobs(run):
    """The map jobs of a frozen run, in seq order."""
    by_sample = {c.sample: c for c in run.inputs.all()}
    inputs = [(i['sample'], by_sample[i['sample']].rows()) for i in run.run_card['inputs']]
    return cards.split_jobs(inputs, run.run_card['exclusions'],
                            run.run_card['split']['files_per_job'])


def _spec_common(run, stage):
    tag = run.tag
    rid = run.run_card_id
    return {
        'kind': 'analysis',
        'stage': stage,
        'runCardId': rid,
        'outDS': f'analysis.{tag.label}.{rid[:12]}.{stage}',
        'vo': 'epic',
        'userName': run.owner,
        'workingGroup': 'EIC',
        'site': run.site,
        # The Analysis share (docs/PANDA_USER_JOBS.md); the run's purpose
        # picks the Validation or User share within it.
        'prodSourceLabel': 'user',
        'taskType': 'anal',
        'processingType': 'epicanalysis',
        'gshare': 'Validation' if run.purpose == 'validation' else 'User',
        'containerImage': tag.image,
        'nCore': 1,
        'memory': int(run.memory_mb),
        'walltimeHours': float(run.walltime_hours),
        'skipScout': True,
        'disableReassign': True,
        'nEventsPerJob': 1,
        'env': {
            'ANALYSIS_STAGE': stage,
            'ANALYSIS_RUN_ID': rid,
            'ANALYSIS_REPO': tag.repo,
            'ANALYSIS_COMMIT': tag.commit,
            'ANALYSIS_CARD': tag.card_path,
            'ANALYSIS_CARD_SHA256': tag.card_sha256,
            'ANALYSIS_PARAMS': cards.params_string(run.run_card['parameters']),
            'ANALYSIS_SCRATCH': cards.scratch_did(run.purpose, run.owner, rid, '').rstrip('/'),
            'ANALYSIS_PUBLISH': cards.publish_did(run.purpose, run.owner, run.campaign, '').rstrip('/'),
            'ANALYSIS_RSE': SCRATCH_RSE,
        },
    }


def build_map_spec(run):
    """The map task of a frozen run: one job per manifest row, each
    running the card's map stage on its files and uploading the partial
    to the run's scratch."""
    if not run.run_card_id:
        raise cards.CardError('freeze the run before building its task')
    jobs = run_jobs(run)
    spec = _spec_common(run, 'map')
    base = f'analysis_{run.run_card_id[:12]}_map'
    spec.update({
        'nJobs': len(jobs),
        'exec': f'bash payload/analysis_run.sh %RNDM=0 {base}',
        'csvBase': base,
        'csvRows': [cards.job_row(j) for j in jobs],
    })
    return spec


def build_merge_spec(run):
    """The merge task: one job per sample, once every map job of the run
    has reported its partial. Each job merges its sample's partials, runs
    the card's finalize stage and publishes the plots with their sidecars.
    The run card and the job records travel in the sandbox, so the job
    can write the full result card itself."""
    jobs = run_jobs(run)
    outputs = {o.seq: o for o in run.job_outputs.filter(status='written')}
    missing = [j['seq'] for j in jobs if j['seq'] not in outputs]
    if missing:
        raise cards.CardError(
            f'{len(missing)} of {len(jobs)} map jobs have no written partial '
            f'(first: {missing[:5]}); merge waits for all of them')
    rows, records = [], []
    for k, label in enumerate(dict.fromkeys(j['label'] for j in jobs)):
        mine = [j for j in jobs if j['label'] == label]
        rows.append(';'.join([str(k), label, 'merge']
                             + [f"{j['job']}={outputs[j['seq']].did}" for j in mine]))
        records += [{'seq': j['seq'], 'sample': j['sample'], 'job': j['job'],
                     'pandaid': outputs[j['seq']].pandaid, 'did': outputs[j['seq']].did,
                     'adler32': outputs[j['seq']].adler32, 'files': len(j['dids']),
                     'events_read': outputs[j['seq']].events_read} for j in mine]
    spec = _spec_common(run, 'merge')
    base = f'analysis_{run.run_card_id[:12]}_merge'
    spec.update({
        'nJobs': len(rows),
        'exec': f'bash payload/analysis_run.sh %RNDM=0 {base}',
        'csvBase': base,
        'csvRows': rows,
        'sandboxFiles': {
            'analysis_run_card.json': cards.canonical_json(run.run_card),
            'analysis_card.json': cards.canonical_json(run.tag.card),
            'analysis_jobs.json': cards.canonical_json(records),
            'analysis_run.json': cards.canonical_json({
                'run_id': run.run_card_id, 'purpose': run.purpose, 'owner': run.owner,
                'campaign': run.campaign, 'panda_tasks': run.panda_tasks}),
        },
    })
    return spec


# --- job reports -----------------------------------------------------------

def record_job_outputs(run, rows):
    """Store the partials map jobs reported. Keyed by (run, seq), so a
    resent report updates rather than duplicates. Returns (written,
    refused reasons)."""
    written, refused = 0, []
    n_jobs = len(run_jobs(run))
    scratch = cards.scratch_did(run.purpose, run.owner, run.run_card_id, '')
    for row in rows:
        try:
            seq = int(row['seq'])
        except (KeyError, TypeError, ValueError):
            refused.append('row without a seq')
            continue
        if not 0 <= seq < n_jobs:
            refused.append(f'seq {seq} is not a job of this run')
            continue
        did = str(row.get('did') or '')
        if not did.startswith(scratch):
            refused.append(f'seq {seq}: {did!r} is outside the run\'s scratch')
            continue
        state = str(row.get('status') or 'written')
        if state not in dict(AnalysisJobOutput.STATUS_CHOICES):
            refused.append(f'seq {seq}: unknown status {state!r}')
            continue
        AnalysisJobOutput.objects.update_or_create(
            run=run, seq=seq,
            defaults={'pandaid': row.get('pandaid'), 'did': did,
                      'adler32': str(row.get('adler32') or '')[:8],
                      'bytes': row.get('bytes'), 'files_read': row.get('files_read'),
                      'events_read': row.get('events_read'), 'status': state,
                      'reason': str(row.get('reason') or ''),
                      'reported_at': row.get('reported_at') or timezone.now()})
        written += 1
    return written, refused


def record_result(run, result):
    """Store the result card a merge job published; the run is done when
    every sample's merge has reported."""
    results = dict((run.result or {}).get('samples') or {})
    results[result['sample']] = result
    run.result = {'samples': results}
    labels = {j['label'] for j in run_jobs(run)}
    if labels <= set(results):
        run.status = 'done'
        run.job_outputs.filter(status='written').update(status='merged')
    run.save()
    return run
