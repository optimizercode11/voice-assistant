#!/usr/bin/env python3
"""Real Qwen voice routing + real local-profile Codex in isolated directories.

Run through the CPU guard; this driver never initializes CUDA or starts a model
server. Artifacts and the isolated session database are retained for review.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import agent_config
import agent_tools
import voice_chat

assert os.environ.get("CUDA_VISIBLE_DEVICES") == ""
parser = argparse.ArgumentParser()
parser.add_argument("--workspace", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--url", default="http://vllm:8080")
args = parser.parse_args()
workspace = Path(args.workspace).resolve()
workspace.mkdir(parents=True, exist_ok=False)
projects = {name: workspace/name.lower() for name in ("Backend", "Tests")}
for directory in projects.values():
    directory.mkdir()
config = agent_config.load(ROOT/"config/host.toml")
config.retrieval.enabled = False
config.mcp = [agent_config.MCPServerConfig(name="codex", command=sys.executable,
    args=[str(ROOT/"tools/mcp_codex_sessions.py"), "--cwd", str(workspace),
          "--state-dir", str(workspace/"state"), "--push"], timeout_seconds=10),
    agent_config.MCPServerConfig(name="workspace", command=sys.executable,
    args=[str(ROOT/"tools/mcp_workspace.py"), "--cwd", str(workspace)], timeout_seconds=10)]
report = {"workspace": str(workspace), "voice_turns": [], "events": [], "checks": {}}
registry = agent_tools.Registry.build(config)
def subscribe():
    registry.subscribe(lambda server, method, params: report["events"].append({"server": server, "method": method, "params": params}))
subscribe()
def save():
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
def tool(name, **kw):
    result = registry.execute("mcp__codex__"+name, kw)
    assert result.ok, result.public()
    return json.loads(result.content)
def voice(text, expected, context="acceptance_a"):
    print("VOICE:", text, flush=True)
    result = voice_chat.turn(args.url, json.dumps({"context_id": context, "messages": [{"role": "user", "content": text}]}),
                             lambda: False, registry=registry, limits=config.limits)
    report["voice_turns"].append({"request": text, "context_id": context, **result})
    save()
    used = {r["name"] for r in result["tools"] if r["ok"]}
    assert expected in used, result
    assert not result["controls"].get("pause_listening"), result
    print("REPLY:", result["text"], flush=True)
    return result
def wait_state(name, expected, seconds=180):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        row = tool("status", session=name)["session"]
        if row["state"] in expected:
            return row
        if row["state"] in {"failed", "unknown"}:
            raise AssertionError(row)
        time.sleep(1)
    raise AssertionError(f"{name} did not reach {expected}: {row}")
try:
    voice(f"Create a Codex session named Backend in {projects['Backend']}. Do not start work yet.", "mcp__codex__create_session")
    voice(f"Create a Codex session named Tests in {projects['Tests']}. Do not start work yet.", "mcp__codex__create_session", "acceptance_b")
    a = tool("status", context_id="acceptance_a")["session"]
    b = tool("status", context_id="acceptance_b")["session"]
    assert a["name"] == "Backend" and b["name"] == "Tests"
    assert a["cwd"] == str(projects["Backend"]) and b["cwd"] == str(projects["Tests"])
    voice("Switch the selected Codex session to Backend.", "mcp__codex__select_session")
    voice("Tell the selected Codex session to implement arithmetic.py with add(a,b) returning a+b and test it with Python assertions including negative numbers. Work only in its project directory.", "mcp__codex__send")
    voice("Tell the selected Codex session to implement text_tools.py with reverse(text) returning the reversed string and test it with Python assertions including the empty string. Work only in its project directory.", "mcp__codex__send", "acceptance_b")
    a = wait_state("Backend", {"idle"})
    b = wait_state("Tests", {"idle"})
    assert a["thread_id"] and b["thread_id"] and a["thread_id"] != b["thread_id"]
    for name, file, function, inputs, expected in [
            ("Backend", "arithmetic.py", "add", (-2, 7), 5),
            ("Tests", "text_tools.py", "reverse", ("abc",), "cba")]:
        path = projects[name]/file
        spec = importlib.util.spec_from_file_location("acceptance_"+name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert getattr(module, function)(*inputs) == expected
        other = projects["Tests" if name == "Backend" else "Backend"]
        assert not (other/file).exists(), "Cross-session file contamination"
    report["checks"]["two_real_coding_sessions"] = [a, b]
    voice("What is the current status of the Backend Codex session?", "mcp__codex__status")
    # Foreground sleep keeps both turns alive long enough to exercise steering
    # and targeted interruption. The manager disables nested model sessions.
    hold = "Use the shell to run python3 -c 'import time; time.sleep(40)' in the foreground, then report finished. Do not start a background process or change any files."
    tool("send", session="Backend", instruction=hold, context_id="acceptance_a")
    tool("send", session="Tests", instruction=hold, context_id="acceptance_b")
    voice("Steer the running Backend Codex task: after waiting, keep the final report to one sentence.", "mcp__codex__steer")
    voice("Cancel the Backend Codex task now. Keep listening and leave Tests running.", "mcp__codex__interrupt")
    wait_state("Backend", {"interrupted"}, 20)
    still_working = tool("status", session="Tests")["session"]
    assert still_working["state"] == "working", still_working
    tool("interrupt", session="Tests")
    wait_state("Tests", {"interrupted"}, 20)
    report["checks"]["targeted_steering_and_cancellation"] = True
    old_thread = a["thread_id"]
    registry.close()
    registry = agent_tools.Registry.build(config)
    subscribe()
    restored = tool("status", context_id="acceptance_a")["session"]
    assert restored["thread_id"] == old_thread and restored["name"] == "Backend"
    tool("send", instruction="Read arithmetic.py and report add(4,5). Do not change files.", context_id="acceptance_a")
    resumed = wait_state("Backend", {"idle"})
    assert resumed["thread_id"] == old_thread
    report["checks"]["persistent_thread_and_focus"] = resumed
    (workspace/"note.txt").write_text("DIRECT_WORKSPACE_ACCEPTANCE\n")
    response = voice("Read note.txt from your workspace directly.", "mcp__workspace__read_file")
    assert not any(r["name"].startswith("mcp__codex__") for r in response["tools"])
    report["checks"]["basic_work_stays_direct"] = True
    report["passed"] = True
    save()
    print("LIVE CODEX SESSION ACCEPTANCE PASS", flush=True)
except BaseException as error:
    report["passed"] = False
    report["error"] = str(error)
    save()
    raise
finally:
    registry.close()
