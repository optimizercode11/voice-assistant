import sys,json
sys.path.insert(0,'/mnt/data/voice-gpu4-20260920-b/tools')
import voice_chat
messages=[]
for i in range(60):messages.extend([{'role':'user','content':f'fact{i}'},{'role':'assistant','content':'noted'}])
messages.append({'role':'user','content':'Remember the first fact'})
try:voice_chat.parse_messages(json.dumps({'messages':messages}))
except voice_chat.ChatError as e:
 assert e.status==400
 print('Observed: existing bridge rejects121-message saved conversation before model receives it')
 sys.exit(1)
raise SystemExit('reproduction failed: old bridge unexpectedly accepted history')
