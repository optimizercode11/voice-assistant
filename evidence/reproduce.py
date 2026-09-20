import pathlib,sys
p=pathlib.Path('/mnt/voice-assistant/deploy/voice-stack-gpu2.service')
s=p.read_text()
assert 'Conflicts=q38-server.service' in s
print('Legacy voice deployment conflicts with the independent q38 service and targets GPU2',flush=True)
sys.exit(1)
