#!/usr/bin/env python3
"""Prepare a PanDA getJobs guard for terminal worker parents; never install.

This changes new dispatch only. Existing job updates and output registration
are unchanged. Installation belongs to the PanDA service maintainer.
"""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path

OLD = '''                                # start transaction
                                self.conn.begin()
                                # pre-lock
'''
NEW = '''                                # start transaction
                                self.conn.begin()
                                # Lock the parent before changing any candidate job.
                                if harvester_id is not None and worker_id is not None:
                                    self.cur.execute(
                                        "SELECT status FROM ATLAS_PANDA.Harvester_Workers "
                                        "WHERE harvesterID=:harvesterID AND workerID=:workerID FOR UPDATE" + comment,
                                        {":harvesterID": harvester_id, ":workerID": worker_id},
                                    )
                                    dispatch_parent = self.cur.fetchone()
                                    if dispatch_parent is not None and dispatch_parent[0] in (
                                        "finished", "failed", "cancelled", "missed"
                                    ):
                                        tmp_log.warning(
                                            f"Refusing new dispatch to terminal worker "
                                            f"harvesterID={harvester_id} workerID={worker_id} "
                                            f"status={dispatch_parent[0]}"
                                        )
                                        self._rollback()
                                        return retJobs, nSent
                                # pre-lock
'''


def prepare_source(source):
    if NEW not in source:
        if source.count(OLD) != 1:
            raise ValueError('Unsupported getJobs transaction context')
        source = source.replace(OLD, NEW, 1)
    ast.parse(source)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source_path, output = args.source.resolve(), args.output.resolve()
    if output == source_path.parent or source_path.parent in output.parents:
        parser.error('Output must be outside the source module directory')
    original = source_path.read_text()
    patched = prepare_source(original)
    for name in [source_path.name, 'terminal-worker-dispatch.patch', 'manifest.json']:
        target = (output / name).resolve()
        if target == source_path or source_path.parent in target.parents:
            parser.error('Output symlinks must not point into the source module directory')
    output.mkdir(parents=True, exist_ok=True)
    (output / source_path.name).write_text(patched)
    name = 'pandaserver/taskbuffer/db_proxy_mods/' + source_path.name
    diff = ''.join(difflib.unified_diff(
        original.splitlines(True), patched.splitlines(True),
        fromfile='a/' + name, tofile='b/' + name))
    (output / 'terminal-worker-dispatch.patch').write_text(diff)
    manifest = {'source': str(source_path), 'installed': False,
                'before_sha256': hashlib.sha256(original.encode()).hexdigest(),
                'after_sha256': hashlib.sha256(patched.encode()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
