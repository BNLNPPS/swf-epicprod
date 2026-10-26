#!/usr/bin/env python3
"""Build a Perlmutter-only ES batching candidate from exact approved native9532.
Does not publish or activate assets. Existing native runtime bundles stay intact.
"""
import argparse,ast,gzip,hashlib,io,json,tarfile
from pathlib import Path

BASELINE='9532f30b41a8b1c8cfcb730eb325933c71c486454ed4365cd3e2d8b625a27ae2'

def replace_once(text,old,new):
    if text.count(old)!=1:raise ValueError('Unexpected frozen pilot text: '+old[:80])
    return text.replace(old,new)

def replace_method(text,class_name,method,body):
    tree=ast.parse(text);cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==class_name)
    node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==method)
    lines=text.splitlines(keepends=True)
    return ''.join(lines[:node.lineno-1])+body+'\n'+''.join(lines[node.end_lineno:])

def build(baseline,output):
    assert hashlib.sha256(baseline.read_bytes()).hexdigest()==BASELINE,'Wrong native baseline'
    with tarfile.open(baseline) as archive:
        members=archive.getmembers();data={m.name:archive.extractfile(m).read() for m in members if m.isfile()}
    changed={}
    def put(name,text):
        ast.parse(text);key='pilot3/'+name;changed[key]={'before':hashlib.sha256(data.get(key,b'')).hexdigest(),'after':hashlib.sha256(text.encode()).hexdigest()};data[key]=text.encode()
    module=Path(__file__).resolve().parents[1]/'pilot/native-canary/es_batch.py'
    put('pilot/user/epic/es_batch.py',module.read_text())
    key='pilot/eventservice/esprocess/esprocess.py';s=data['pilot3/'+key].decode()
    s=replace_once(s,'    def get_event_range_to_payload(self) -> list:', '    def get_event_range_to_payload(self, max_ranges=1) -> list:')
    # An empty cache is refilled with several ranges per core in one server
    # acquisition (es_batch.acquire_count), not one per core.
    s=replace_once(s,'        if not self.event_ranges_cache:\n            event_ranges = self.get_event_ranges()\n','        if not self.event_ranges_cache:\n            from pilot.user.epic.es_batch import acquire_count\n            event_ranges = self.get_event_ranges(acquire_count(self.corecount))\n')
    s=replace_once(s,'            event_range = self.event_ranges_cache.pop(0)\n            return event_range','            if max_ranges == 1:\n                return self.event_ranges_cache.pop(0)\n            count = min(max_ranges, len(self.event_ranges_cache))\n            return [self.event_ranges_cache.pop(0) for _ in range(count)]')
    s=replace_once(s,"            if message.startswith('ERR'):","            if message.startswith('EPICPROD_ES_RESULT_V1 '):\n                from pilot.user.epic.es_batch import decode_result\n                return decode_result(message, self.__payload['job'].jobid)\n            if message.startswith('ERR'):")
    # The job's ranges from a cache filled at its start and refilled by its own
    # thread, straight from the server (es_batch.Prefetch), when
    # EPICPROD_ES_PREFETCH_RANGES is set; otherwise the refill below.
    s=replace_once(s,'        logger.debug(f"number of cached event ranges: {len(self.event_ranges_cache)}")\n','        from pilot.user.epic.es_batch import prefetch_take\n        taken = prefetch_take(self, max_ranges)\n        if taken is not None:\n            return taken\n        logger.debug(f"number of cached event ranges: {len(self.event_ranges_cache)}")\n')
    s=replace_once(s,"        self.init()\n        logger.debug('initialization finished.')\n","        self.init()\n        from pilot.user.epic.es_batch import prefetch_start\n        prefetch_start(self, self.__payload['job'], self.__stop)\n        logger.debug('initialization finished.')\n")
    s=replace_once(s,'                event_ranges = self.get_event_range_to_payload()',"                from pilot.user.epic.es_batch import request_count\n                event_ranges = self.get_event_range_to_payload(request_count(message))")
    s=replace_once(s,'            event_ranges = self.get_event_ranges_hook(num_ranges)',"            from pilot.user.epic.es_batch import enabled, throttle_acquire\n            if enabled():\n                throttle_acquire(self)\n                num_ranges = max(num_ranges, 256)\n            event_ranges = self.get_event_ranges_hook(num_ranges)")
    put(key,s)
    key='pilot/eventservice/workexecutor/plugins/genericexecutor.py';s=data['pilot3/'+key].decode()
    s=replace_method(s,'GenericExecutor','tarzip_output_es','''    def tarzip_output_es(self):
        from pilot.user.epic.es_batch import enabled, pack_reports
        if not enabled():
            raise RuntimeError('This candidate must run with its explicit batch-channel gate')
        return pack_reports(self.__queued_out_messages)
''')
    s=replace_method(s,'GenericExecutor','update_finished_event_ranges','''    def update_finished_event_ranges(self, out_messages, output_file, fsize, checksum, storage_id):
        from pilot.user.epic.es_batch import update_finished
        return update_finished(self, out_messages, output_file, fsize, checksum, storage_id)
''')
    # The forced stage-out at the payload's end is on the critical path of the
    # job's end, and comes after the executor has stopped its communication
    # manager: its finished ranges go to the server directly, without the
    # receipt archive and its upload (es_batch.report_now). With
    # EPICPROD_ES_REPORT_AT_ONCE=1 every pass (each five seconds) does the
    # same, so a close is credited as it stands; otherwise periodic stage-outs
    # keep their archives.
    s=replace_once(s,"        job = self.get_job()\n        if self.__queued_out_messages:\n            if force or","        job = self.get_job()\n        from pilot.user.epic.es_batch import report_at_once, report_now\n        if self.__queued_out_messages and (force or report_at_once()):\n            count = len(self.__queued_out_messages)\n            report_now(self, self.__queued_out_messages, final=force)\n            logger.info(f'reported {count} messages straight to the server' + (' at the end' if force else ''))\n            return\n        if self.__queued_out_messages:\n            if force or")
    put(key,s)
    key='pilot/info/configinfo.py';s=data['pilot3/'+key].decode()
    s=replace_once(s,'import ast','import ast\nfrom pilot.user.epic.es_batch import stageout_gap')
    s=replace_once(s,"            'es_stageout_gap': 601,  # in seconds, for testing: FIXME LATER,","            'es_stageout_gap': stageout_gap(),\n            'zip_time_gap': stageout_gap(),")
    put(key,s)
    output.mkdir(parents=True,exist_ok=False)
    path=output/'pilot3-native.tar.gz'
    with path.open('wb') as raw,gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as zipped,tarfile.open(fileobj=zipped,mode='w') as archive:
        for m in members:
            if m.isfile():m.size=len(data[m.name]);archive.addfile(m,io.BytesIO(data.pop(m.name)))
            else:archive.addfile(m)
        for name,content in data.items():
            m=tarfile.TarInfo(name);m.mode=0o644;m.size=len(content);archive.addfile(m,io.BytesIO(content))
    result={'baseline_sha256':BASELINE,'pilot_archive_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'files_changed':changed,'scope':'Perlmutter ES batch-channel candidate only; unpublished'}
    (output/'build.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.baseline,a.output)
