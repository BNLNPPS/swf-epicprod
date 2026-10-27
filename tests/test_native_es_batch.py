"""Bounded native ES wire, reporting and dispatch regression checks; no service calls."""
import importlib.util,json,os,sys,tarfile,tempfile,threading,time,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('epic_es_batch',ROOT/'pilot/native-canary/es_batch.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
sys.path.insert(0,str(ROOT/'swf_epicprod/payload/es'))
import es_harness as h

def ids(first=1,n=250):return [f'41299-6010000-12345-{i}-1' for i in range(first,first+n)]
def wire(ranges):return b.RESULT_PREFIX+json.dumps({'ids':ranges,'output':'/work/unit.json','wall':120})

class NativeBatchTests(unittest.TestCase):
 def setUp(self):self.env=patch.dict(os.environ,{'EPICPROD_SWITCH_ES_BATCH_CHANNEL':'1'});self.env.start()
 def tearDown(self):self.env.stop()
 def test_one_message_retains_every_owned_range(self):
  result=b.decode_result(wire(ids()),6010000)
  self.assertEqual(result['ids'],ids());self.assertEqual(result['id'],ids()[0])
  for bad in [ids()+ids()[:1],ids()[:-1]+['41299-99-12345-250-1'],ids()[:2]+ids(4,2)]:
   with self.assertRaises(ValueError):b.decode_result(wire(bad),6010000)
 def test_ten_event_ranges_decode_and_count_events(self):
  tens=[f'41431-6010000-12345-{i}-1' for i in range(951,1051,10)]
  result=b.decode_result(b.RESULT_PREFIX+json.dumps({'ids':tens,'output':'/w/unit.json','wall':1,'events':100}),6010000)
  self.assertEqual(result['ids'],tens);self.assertEqual(result['events'],100)
  self.assertEqual(b.decode_result(wire(ids()),6010000)['events'],250)
  for bad in [tens[:2]+tens[3:],[tens[1],tens[0]],tens[:1]+tens[:1]]:
   with self.assertRaises(ValueError):b.decode_result(b.RESULT_PREFIX+json.dumps({'ids':bad,'output':'/w/u.json'}),6010000)
  e=types.SimpleNamespace(job=types.SimpleNamespace(nevents=0));e.get_job=lambda:e.job
  e.update_events=lambda body:{'Returns':[True]*len(json.loads(body['eventRanges'])[0]['eventRanges'])+[False]}
  b.update_finished(e,[result],'r.tar',1,{},1);self.assertEqual(e.job.nevents,100)
 def test_post_finished_counts_events_of_taken_ranges(self):
  tens=[f'41431-6010000-12345-{i}-1' for i in range(1,41,10)]
  e=types.SimpleNamespace(job=types.SimpleNamespace(nevents=0));e.get_job=lambda:e.job
  with patch.object(b,'_server',return_value={'Returns':[True,True,False,True,False]}):
   b.post_finished(e,[{'id':tens[0],'ids':tens,'events':40}])
  self.assertEqual(e.job.nevents,30)
  with patch.object(b,'_server',return_value={'Returns':[True]}):
   with self.assertRaises(RuntimeError):b.post_finished(e,[{'id':tens[0],'ids':tens,'events':40}])
 def test_config_provider_override_uses_explicit_safe_interval(self):
  with patch.dict(os.environ,{'EPICPROD_ES_STAGEOUT_GAP':'300'}):self.assertEqual(b.stageout_gap(),300)
  with patch.dict(os.environ,{'EPICPROD_ES_STAGEOUT_GAP':'60'}):
   with self.assertRaises(ValueError):b.stageout_gap()
  with patch.dict(os.environ,{'EPICPROD_ES_STAGEOUT_GAP':''}):self.assertEqual(b.stageout_gap(),601)
 def test_bounded_requests_and_legacy_default(self):
  self.assertEqual(b.request_count('Ready for events: 256'),256)
  self.assertEqual(b.request_count(f'Ready for events: {h.REQUEST_RANGES}'),h.REQUEST_RANGES)
  for s in ['Ready for events: 0','Ready for events: 513']:
   with self.assertRaises(ValueError):b.request_count(s)
  with patch.dict(os.environ,{'EPICPROD_SWITCH_ES_BATCH_CHANNEL':'0'}):self.assertEqual(b.request_count('Ready for events'),1)
 def test_bulk_updates_preserve_all_ids_and_bound_request_size(self):
  class Executor:
   def __init__(self):self.calls=[];self.job=types.SimpleNamespace(nevents=0)
   def get_job(self):return self.job
   def update_events(self,body):
    ranges=json.loads(body['eventRanges'])[0]['eventRanges'];self.calls.extend([ranges]);return {'Returns':[True]*len(ranges)}
  e=Executor();messages=[{'id':ids(i*250+1)[0],'ids':ids(i*250+1)} for i in range(250)]
  b.update_finished(e,messages,'report.tar',100,{'adler32':'12345678'},1)
  self.assertEqual(e.job.nevents,62500);self.assertEqual(len(e.calls),7)
  actual=[r['eventRangeID'] for batch in e.calls for r in batch]
  self.assertEqual(len(set(actual)),62500);self.assertEqual(actual,ids(1,62500))
  self.assertTrue(all(len(batch)<=10000 for batch in e.calls))
 def test_rejected_batch_does_not_advance_pilot_count(self):
  e=types.SimpleNamespace(job=types.SimpleNamespace(nevents=0));e.get_job=lambda:e.job;e.update_events=lambda _: {'Returns':[False]}
  with self.assertRaises(RuntimeError):b.update_finished(e,[{'id':ids()[0]}],'r.tar',1,{},1)
  self.assertEqual(e.job.nevents,0)
 def test_duplicate_event_receipts_are_packed_once_per_unit(self):
  with tempfile.TemporaryDirectory() as d:
   old=os.getcwd();os.chdir(d)
   try:
    Path('one.json').write_text('{}');Path('two.json').write_text('{}')
    messages=[{'id':rid,'output':str(Path('one.json').resolve())} for rid in ids()]
    messages += [{'id':rid,'output':str(Path('two.json').resolve())} for rid in ids(251)]
    kept,archive=b.pack_reports(messages)
    self.assertEqual(len(kept),500);self.assertEqual(messages,[])
    with tarfile.open(archive) as f:self.assertEqual(len(f.getmembers()),2)
   finally:os.chdir(old)
 def test_payload_retains_all_ranges_in_one_received_batch(self):
  responses=[json.dumps([{'eventRangeID':rid} for rid in ids(1,256)]).encode(),b'No more events'];sent=[]
  class Socket:
   def send_raw(self,message):sent.append(message)
   def try_recv_raw(self):r=responses.pop(0);return len(r),r
  with patch.dict(sys.modules,{'yampl':types.SimpleNamespace(ClientSocket=lambda *args:Socket())}):feed=h.ChannelFeed('offline')
  self.assertEqual([feed.ask()['eventRangeID'] for _ in range(256)],ids(1,256))
  self.assertEqual(len(sent),1);self.assertEqual(sent[0],f'Ready for events: {h.REQUEST_RANGES}'.encode());self.assertIsNone(feed.ask())
  stats=feed.stats();self.assertEqual((stats['requests'],stats['ranges']),(1,256));self.assertIsNotNone(stats['answer_s_median'])
 def test_fragmented_full_buffer_refills_in_one_bounded_batch(self):
  class Feed:
   exhausted=False
   def __init__(self):self.n=0
   def ask(self):
    self.n+=1;n=self.n;e=2*n-1 if n<=32 else 2*(n-32) if n<=64 else n
    return {'eventRangeID':str(e),'startEvent':e,'lastEvent':e,'LFN':'f'}
  feed=Feed();pool=h.Pool(feed,lookahead=32,demand_batch=256)
  try:
   stop=time.monotonic()+2
   while len(pool)<32 and time.monotonic()<stop:time.sleep(.01)
   self.assertEqual(pool.take_unit(250,min_events=16),[])
   pool.want();stop=time.monotonic()+2
   while len(pool)<64 and time.monotonic()<stop:time.sleep(.01)
   self.assertEqual(len(pool),64);self.assertEqual(feed.n,64)
   self.assertEqual(len(pool.take_unit(250,min_events=16)),64)
  finally:feed.exhausted=True;pool.wanted.set();pool.thread.join(1)
 def test_fair_dispatch_does_not_starve_higher_numbered_cores(self):
  class Slot:
   def __init__(self,i):self.index=i;self.idle_since=0;self.given=0;self.unit=None
   def free(self):return True
   def give(self,spec):self.given+=1;self.idle_since=tick+1
  class Pool:
   def __init__(self):self.left=0;self.event=0
   def take_unit(self,*args,**kwargs):
    if not self.left:return []
    self.left-=1;self.event+=1;return [{'eventRangeID':str(self.event),'startEvent':self.event,'lastEvent':self.event,'LFN':'f'}]
   def want(self):pass
  pool=Pool();slots=[Slot(i) for i in range(128)]
  for tick in range(16):
   pool.left=16;h.dispatch_available(slots,pool,250,16,'','row','steer','sample',fair=True)
  self.assertEqual([s.given for s in slots],[2]*128)

if __name__=='__main__':unittest.main()
