import concurrent.futures, os, pathlib, sys, threading, time, unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'tools'))
import mcp_client
assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
FIXTURE=str(pathlib.Path(__file__).parent/'fixtures/fake_mcp_server.py')
class TransportTests(unittest.TestCase):
 def test_parallel_requests_keep_ids_and_lines_separate(self):
  server=mcp_client.MCPServer('test',sys.executable,[FIXTURE],timeout=5)
  server.start()
  original=server._write; activity=threading.Lock(); active=0;peak=0
  def write(message):
   nonlocal active,peak
   with activity:active+=1;peak=max(peak,active)
   try:
    time.sleep(.003)
    original(message)
   finally:
    with activity:active-=1
  barrier=threading.Barrier(12)
  def request(i):
   barrier.wait()
   server.notify('notifications/test',{'session':i})
   result=server.request('tools/call',{'name':f'session-{i}','arguments':{'padding':'x'*20000}})
   return result['content'][0]['text']
  try:
   with patch.object(server,'_write',side_effect=write):
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
     results=list(pool.map(request,range(12)))
   self.assertEqual(results,[f'session-{i} ran' for i in range(12)])
   self.assertEqual(peak,1,'RPC writes must never overlap between sessions')
   self.assertFalse(server.pending)
  finally:server.stop()
 def test_failed_send_does_not_leave_pending_request(self):
  server=mcp_client.MCPServer('missing','/no/such/program')
  with self.assertRaises(mcp_client.MCPError):server.request('test',{})
  self.assertEqual(server.pending,{})
if __name__=='__main__':unittest.main(verbosity=2)
