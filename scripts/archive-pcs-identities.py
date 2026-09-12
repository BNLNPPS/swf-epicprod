#!/usr/bin/env python3
"""Pull a consistent PCS identity/history snapshot onto an independent receiver.

Uses a libpq service (read-only pcs_archive_reader). No application environment
or owner credentials. Publishes a new checksummed archive without overwriting
or expiring older files. Storage retention must be enforced by the receiver.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import psycopg
from psycopg import sql

TABLES = ('pcs_physics_category', 'pcs_physics_tag', 'pcs_evgen_tag', 'pcs_simu_tag',
          'pcs_reco_tag', 'pcs_background_tag', 'pcs_physics_config',
          'pcs_dataset', 'pcs_identity_history')


def archive(service, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    name = f'pcs-identities-{stamp}-{uuid.uuid4().hex}.jsonl.gz'
    final = destination / name
    counts = {}
    fd, temporary = tempfile.mkstemp(prefix='.pcs-inprogress-', dir=destination)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as out:
                def emit(value):
                    out.write((json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n').encode())
                with psycopg.connect(service=service) as connection:
                    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    emit({'format': 'pcs-permanent-identities-v1', 'captured_at': stamp})
                    for table in TABLES:
                        order = 'digit' if table == 'pcs_physics_category' else 'id'
                        counts[table] = 0
                        with connection.cursor(name='pcs_archive') as cursor:
                            cursor.execute(sql.SQL('SELECT to_jsonb(t) FROM public.{} t ORDER BY {}').format(
                                sql.Identifier(table), sql.Identifier(order)))
                            for row, in cursor:
                                emit({'table': table, 'row': row})
                                counts[table] += 1
                    emit({'complete': True, 'counts': counts})
            raw.flush()
            os.fsync(raw.fileno())
        # Validate the gzip stream and trailer before publishing it.
        with gzip.open(temporary, 'rt') as check:
            last = None
            for line in check:
                last = json.loads(line)
            if last != {'complete': True, 'counts': counts}:
                raise RuntimeError('Archive verification failed')
        digest = hashlib.sha256()
        with open(temporary, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        os.chmod(temporary, 0o440)
        os.link(temporary, final)  # exclusive publication; never replace a file
        with open(str(final) + '.sha256', 'x') as checksum:
            checksum.write(f'{digest.hexdigest()}  {name}\n')
            checksum.flush()
            os.fsync(checksum.fileno())
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)
    return {'archive': str(final), 'sha256': digest.hexdigest(), 'counts': counts}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, help='libpq read-only service on the receiver')
    parser.add_argument('--destination', required=True, help='receiver-owned retained archive directory')
    args = parser.parse_args()
    print(json.dumps(archive(args.service, args.destination)))
