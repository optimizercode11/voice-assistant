"""Restore captured voice/Qwen3.6 service states after a failed switchover."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
states = json.loads((root/'rollback/service-states.json').read_text())
for unit in states:
    saved = root/'rollback'/unit
    if saved.is_file():
        (Path.home()/'.config/systemd/user'/unit).write_bytes(saved.read_bytes())
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
for unit, state in states.items():
    if state['enabled'] == 'enabled':
        subprocess.run(['systemctl', '--user', 'enable', unit], check=True)
    if state['active'] == 'active':
        subprocess.run(['systemctl', '--user', 'start', unit], check=True)
