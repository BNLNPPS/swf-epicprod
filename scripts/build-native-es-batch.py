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
    s=replace_once(s,"            if message.startswith('ERR'):","            if message.startswith('EPICPROD_ES_RESULT_V1 '):\n                from pilot.user.epic.es_batch import decode_result\n                return decode_result(message, self.__payload['job'].jobid)\n            if message.startswith('EPICPROD_ES_FAILED_V1 '):\n                from pilot.user.epic.es_batch import decode_failed\n                return decode_failed(message, self.__payload['job'].jobid)\n            if message.startswith('ERR'):")
    # A payload message the pilot cannot handle is logged and its ranges left
    # to the server, which releases them with the job's other unfinished
    # ranges; it never stops the ES process, which would end the payload's feed.
    s=replace_once(s,'            raise RunPayloadFailure(f"Failed to handle out message: {e}") from e','            from pilot.user.epic.es_batch import enabled\n            if not enabled():\n                raise RunPayloadFailure(f"Failed to handle out message: {e}") from e\n            logger.error(f"payload message not handled, its ranges left to the server: {e}; {message[:300]}")')
    # The job's ranges from a cache filled at its start and refilled by its own
    # thread, straight from the server (es_batch.Prefetch), when
    # EPICPROD_ES_PREFETCH_RANGES is set; otherwise the refill below.
    s=replace_once(s,'        logger.debug(f"number of cached event ranges: {len(self.event_ranges_cache)}")\n','        from pilot.user.epic.es_batch import prefetch_take\n        taken = prefetch_take(self, max_ranges)\n        if taken is not None:\n            return taken\n        logger.debug(f"number of cached event ranges: {len(self.event_ranges_cache)}")\n')
    # The payload is told, through the environment it inherits, that this pilot
    # takes a failed unit as one message (es_batch.advertise).
    s=replace_once(s,"        self.init()\n        logger.debug('initialization finished.')\n","        from pilot.user.epic.es_batch import advertise, prefetch_start\n        advertise()\n        self.init()\n        prefetch_start(self, self.__payload['job'], self.__stop)\n        logger.debug('initialization finished.')\n")
    s=replace_once(s,'                event_ranges = self.get_event_range_to_payload()',"                from pilot.user.epic.es_batch import request_count\n                event_ranges = self.get_event_range_to_payload(request_count(message))")
    s=replace_once(s,'            event_ranges = self.get_event_ranges_hook(num_ranges)',"            from pilot.user.epic.es_batch import enabled, throttle_acquire\n            if enabled():\n                throttle_acquire(self)\n                num_ranges = max(num_ranges, 256)\n            event_ranges = self.get_event_ranges_hook(num_ranges)")
    put(key,s)
    # Inputs staged at the site ahead of the job are linked in and marked
    # delivered; only the rest go to the transfer (es_batch.link_site_staged).
    key='pilot/control/data.py';s=data['pilot3/'+key].decode()
    s=replace_once(s,'            client.transfer(job.indata, activity=activity, **kwargs)\n','            from pilot.user.epic.es_batch import link_site_staged\n            remaining = link_site_staged(job.indata, workdir)\n            if remaining:\n                client.transfer(remaining, activity=activity, **kwargs)\n')
    put(key,s)
    key='pilot/eventservice/workexecutor/plugins/genericexecutor.py';s=data['pilot3/'+key].decode()
    s=replace_method(s,'GenericExecutor','tarzip_output_es','''    def tarzip_output_es(self):
        from pilot.user.epic.es_batch import enabled, pack_reports
        if not enabled():
            raise RuntimeError('This candidate must run with its explicit batch-channel gate')
        return pack_reports(self.__queued_out_messages)
''')
    # Failed ranges are queued for the executor's pass and sent in bulk
    # (es_batch.report_failed), never a server call each in the ES process's
    # loop, which answers the payload's range requests (job 6254767).
    s=replace_method(s,'GenericExecutor','update_failed_event_ranges','''    def update_failed_event_ranges(self, out_messages):
        from pilot.user.epic.es_batch import queue_failed
        queue_failed(self, out_messages)
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
    s=replace_once(s,"        job = self.get_job()\n        if self.__queued_out_messages:\n            if force or","        job = self.get_job()\n        from pilot.user.epic.es_batch import report_at_once, report_failed, report_now\n        if self.__queued_out_messages and (force or report_at_once()):\n            count = len(self.__queued_out_messages)\n            report_now(self, self.__queued_out_messages, final=force)\n            logger.info(f'reported {count} messages straight to the server' + (' at the end' if force else ''))\n            report_failed(self, final=force)\n            return\n        report_failed(self, final=force)\n        if self.__queued_out_messages:\n            if force or")
    put(key,s)
    # The job's log archive goes beside the renamed work directory, in the
    # pilot's home, not in the process's current directory: the ES executor
    # leaves the pilot in the job's work directory (baseexecutor.set_payload),
    # which create_log renames away before tar writes, so no archive was made
    # and the log stage-out failed (job 6258483, pilot error 1165). Tar's own
    # exit status decides, where `; echo $?` had reported success; its exit 1
    # (a file changed while read) still leaves a whole archive.
    key='pilot/control/data.py';s=data['pilot3/'+key].decode()
    s=replace_once(s,'    fullpath = os.path.join(current_dir, logfile_name)  # /some/path/to/dirname/log.tgz','    fullpath = os.path.join(pilot_home, logfile_name)  # beside the renamed work directory')
    s=replace_once(s,'        cmd = f"pwd;tar cvfz {fullpath} {tarball_name} --dereference --one-file-system; echo $?"','        cmd = f"tar cvfz {fullpath} {tarball_name} --dereference --one-file-system || [ $? -eq 1 ]"')
    put(key,s)
    # An Event Service job's log is its harness's files, logs cut to head and
    # tail and failed units whole, not the detector data copied into each slot
    # (es_batch.trim_log_dir; 149 MB to 40 MB for log test 41452).
    key='pilot/user/epic/common.py';s=data['pilot3/'+key].decode()
    s=replace_once(s,'    remove_root_files(workdir, outputfiles)\n','    remove_root_files(workdir, outputfiles)\n\n    from pilot.user.epic.es_batch import enabled as es_batch_enabled, trim_log_dir\n    if es_batch_enabled():\n        trim_log_dir(workdir)\n')
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
