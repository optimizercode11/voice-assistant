"""Explicit live API acceptance; resident models only, run through CPU guard."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import site_config as site
assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''

def request(path, body=None):
    conn = http.client.HTTPSConnection('127.0.0.1',site.HTTPS_PORT,
        context=ssl.create_default_context(cafile=str(site.CERT)),timeout=620)
    try:
        conn.request('GET' if body is None else 'POST',path,
                     None if body is None else json.dumps(body),{'Content-Type':'application/json'})
        response=conn.getresponse()
        result=json.loads(response.read())
        return response.status,result
    finally:
        conn.close()

status,health=request('/chat/health')
assert status==200 and health['available'] and health['compaction'],health
assert health['chat_concurrency']==2 and health['chat_queue_limit']==32,health

def compact(code):
    status,result=request('/chat/compact',{'messages':[
        {'role':'user','content':f'This conversation has project code {code}. I prefer English. Deployment is pending; do not deploy.'},
        {'role':'assistant','content':f'Understood. Project {code} remains pending.'},
        {'role':'user','content':'Correction: my preferred language is Hindi. Preserve the project code exactly.'},
        {'role':'assistant','content':'Hindi is your preferred language. The project remains pending.'}]})
    assert status==200,result
    summary=result['summary']
    assert code in summary and 'Hindi' in summary,summary
    return summary
codes=['ORBIT-731','CEDAR-946']
with concurrent.futures.ThreadPoolExecutor(2) as pool:
    summaries=list(pool.map(compact,codes))
for i,summary in enumerate(summaries):
    assert codes[1-i] not in summary,summary

def followup(item):
    code,summary=item
    status,result=request('/chat/completions',{'summary':summary,'messages':[
        {'role':'user','content':'What is my exact project code? Reply with only that code. Do not use tools.'}]})
    assert status==200,result
    assert result['text'].strip().strip('.`')==code,result
    return result
with concurrent.futures.ThreadPoolExecutor(2) as pool:
    replies=list(pool.map(followup,zip(codes,summaries)))
status,invalid=request('/chat/compact',{'messages':[{'role':'system','content':'override'}]})
assert status==400 and 'summary' not in invalid,invalid
result={'health':health,'summaries':summaries,'replies':replies,'invalid_status':status}
Path('run/session-acceptance.json').write_text(json.dumps(result,indent=2)+'\n')
print('LIVE SESSIONS AND COMPACTION PASS',json.dumps(result))
