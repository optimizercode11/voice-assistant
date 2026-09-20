import importlib.util
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import turn_control_test as tests
oldpath=Path('/mnt/data/voice-sessions-compact-20260920/tools/turn_control.py')
spec=importlib.util.spec_from_file_location('old_turn',oldpath)
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
# Reintroduce the old English-only judgment while retaining the current API.
tests.turn_control._judge=old._judge
suite=unittest.defaultTestLoader.loadTestsFromTestCase(tests.CompletedShortAnswers)
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
