"""Explicit live API smoke, using resident services only; no Claude is started."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
import voice_stack as stack
assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
health=json.loads(stack.request('/chat/health'))
assert 'mcp__claude__stop' in health['tools'],health
# This command exercises stop, never send; an idle session remains stopped.
reply=json.loads(stack.request('/chat/completions',json.dumps({'messages':[
    {'role':'user','content':'Stop Claude Code now using its stop tool. Keep the microphone listening.'}]})))
assert any(t['name']=='mcp__claude__stop' and t['ok'] for t in reply['tools']),reply
assert not reply['controls'].get('pause_listening'),reply
spoken=stack.request('/tts?format=wav&language=h&voice=hf_alpha&speed=1.2','मुझे समय बताओ।'.encode())
heard=json.loads(stack.request('/stt',spoken))
assert heard['text'] and not heard['turn']['hold'] and not heard['turn']['discard'],heard
result={'stop_reply':reply,'hindi_transcript':heard['text'],'turn':heard['turn']}
Path('run/voice-stability-acceptance.json').write_text(json.dumps(result,indent=2)+'\n')
print('LIVE VOICE STABILITY PASS',json.dumps(result))
