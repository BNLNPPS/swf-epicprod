"""An Event Service job's shipped record, whole: the one reader for every
consumer (the close-out, the crediting tools, the record filer, the job
page's sweep, es-observe).

The harness writes the job's record, ``reports/<job>/es.json``, at least
once a minute, and each close with its units' records as it stands,
``reports/<job>/close-NNNN.json`` (payload/es/es_record.py). ``load``
returns the job's record with the units of every shipped close in its
``done`` (a close that stood) or ``failed`` (one that did not) list, each
unit once, whether or not the job's record still carries them, so readers
see the same record however the harness divides it.
"""
import json


def _get(client, bucket, key):
    return json.loads(client.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8'))


def close_keys(client, bucket, prefix, pandaid):
    """The keys of the job's shipped closes, in close order."""
    keys, token = [], None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': f'{prefix}{pandaid}/close-'}
        if token:
            kwargs['ContinuationToken'] = token
        page = client.list_objects_v2(**kwargs)
        keys += [o['Key'] for o in page.get('Contents') or []]
        if not page.get('IsTruncated'):
            return sorted(keys)
        token = page.get('NextContinuationToken')


def join(record, closes):
    """The job's record with the shipped closes' units joined in. Pure."""
    record = dict(record)
    done, failed = list(record.get('done') or []), list(record.get('failed') or [])
    seen = {u.get('unit_id') for u in done + failed}
    known = {c.get('index') for c in record.get('closes') or []}
    extra_closes = []
    for shipped in closes:
        close = shipped.get('close') or {}
        for unit in shipped.get('units') or []:
            if unit.get('unit_id') in seen:
                continue
            seen.add(unit.get('unit_id'))
            (done if close.get('ok') else failed).append(dict(unit, close=close.get('index')))
        if close.get('index') not in known:
            extra_closes.append(close)
    record['done'], record['failed'] = done, failed
    if extra_closes:
        record['closes'] = sorted(list(record.get('closes') or []) + extra_closes,
                                  key=lambda c: int(c.get('index') or 0))
    return record


def load(client, bucket, prefix, pandaid):
    """The job's record, whole. Raises as the store does when the job has no
    record; an unreadable close object is skipped and named in the record's
    ``close_errors``."""
    record = _get(client, bucket, f'{prefix}{pandaid}/es.json')
    closes, errors = [], []
    for key in close_keys(client, bucket, prefix, pandaid):
        try:
            closes.append(_get(client, bucket, key))
        except Exception as e:                                # noqa: BLE001
            errors.append(f'{key}: {e}')
    record = join(record, closes)
    if errors:
        record['close_errors'] = errors
    return record
