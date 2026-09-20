import importlib.util
from pathlib import Path
path=Path('/mnt/data/voice-sessions-compact-20260920/tools/turn_control.py')
spec=importlib.util.spec_from_file_location('old_turn',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
for phrase in ('42','मुझे समय बताओ।','你好。'):
 verdict=module.completeness(phrase,800)
 print(phrase,verdict,flush=True)
 assert not verdict['hold'], 'A completed numeric/multilingual answer waits indefinitely for another utterance'
