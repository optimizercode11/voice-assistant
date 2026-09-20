#!/usr/bin/env python3
"""CPU-only App Server fixture; accepts the same `app-server` argv as Codex."""
import json
import os
import subprocess
import sys
import threading
import time

write_lock = threading.Lock()
threads = {}
turns = {}
questions = {}
mode = os.environ.get("FAKE_CODEX_MODE", "")
state_path = os.environ.get("FAKE_CODEX_STATE")
if state_path and os.path.exists(state_path):
    with open(state_path) as handle:
        threads.update(json.load(handle))


def persist():
    if state_path:
        with open(state_path, "w") as handle:
            json.dump(threads, handle)


def send(value):
    with write_lock:
        print(json.dumps(value), flush=True)


def reply(request, value):
    send({"id": request["id"], "result": value})


def notify(method, params):
    send({"method": method, "params": params})


def thread_response(thread):
    return {"thread": thread, "model": thread.get("model", "fake-codex"), "modelProvider": thread.get("modelProvider", "fake"),
            "cwd": thread["cwd"], "approvalPolicy": "never", "approvalsReviewer": "user",
            "sandbox": {"type": "dangerFullAccess"}}


def finish(thread_id, turn_id, status="completed", text="SPOKEN: Task completed."):
    turn = turns.get(turn_id)
    if turn is None or turn["status"] != "inProgress":
        return
    turn["status"] = status
    if status == "completed":
        item = {"id": "item-" + turn_id, "type": "agentMessage", "text": text, "phase": "final_answer"}
        turn["items"].append(item)
        notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                  "item": item, "completedAtMs": int(time.time() * 1000)})
    elif status == "failed":
        turn["error"] = {"message": "Fixture turn failed", "codexErrorInfo": None, "additionalDetails": None}
    threads[thread_id]["status"] = {"type": "idle"}
    persist()
    notify("turn/completed", {"threadId": thread_id, "turn": turn})


def after(delay, callback):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()


for line in sys.stdin:
    request = json.loads(line)
    log = os.environ.get("FAKE_CODEX_RPC_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(request) + "\n")
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        if mode == "init_slow":
            time.sleep(0.5)
        if mode == "init_fail":
            send({"id": request["id"], "error": {"code": -32000, "message": "Initialization rejected"}})
            continue
        reply(request, {"userAgent": "fake-codex/1", "platformFamily": "unix", "platformOs": "linux"})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        thread_id = "thread-" + str(len(threads) + 1)
        now = int(time.time())
        thread = {"id": thread_id, "cwd": params.get("cwd", os.getcwd()), "name": None,
                  "preview": "", "model": params.get("model", "fake-codex"),
                  "modelProvider": params.get("modelProvider", "fake"), "createdAt": now, "updatedAt": now,
                  "status": {"type": "idle"}, "turns": [], "ephemeral": False,
                  "cliVersion": "0.155.1", "projectId": None, "sessionId": thread_id, "source": "cli"}
        threads[thread_id] = thread
        persist()
        notify("thread/started", {"thread": thread})
        reply(request, thread_response(thread))
    elif method in ("thread/resume", "thread/read"):
        thread_id = params["threadId"]
        if thread_id not in threads:
            send({"id": request["id"], "error": {"code": -32602, "message": "Unknown thread"}})
        else:
            thread = threads[thread_id]
            reply(request, thread_response(thread) if method == "thread/resume" else {"thread": thread})
    elif method == "thread/name/set":
        threads[params["threadId"]]["name"] = params["name"]
        persist()
        reply(request, {})
    elif method == "thread/archive":
        threads[params["threadId"]]["archived"] = True
        persist()
        reply(request, {})
    elif method == "turn/start":
        thread_id = params["threadId"]
        turn_id = "turn-" + str(sum(len(t["turns"]) for t in threads.values()) + 1)
        text = " ".join(item.get("text", "") for item in params.get("input", []))
        turn = {"id": turn_id, "items": [], "status": "inProgress", "error": None}
        turns[turn_id] = turn
        threads[thread_id]["turns"].append(turn)
        threads[thread_id]["status"] = {"type": "active", "activeFlags": []}
        persist()
        reply(request, {"turn": turn})
        notify("turn/started", {"threadId": thread_id, "turn": turn})
        if "question" in text.lower():
            request_id = "question-" + turn_id
            questions[request_id] = (thread_id, turn_id)
            send({"id": request_id, "method": "item/tool/requestUserInput",
                  "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "question-item",
                             "isBlocking": True, "questions": [{"id": "choice", "header": "Choice",
                             "question": "Which approach should I use?", "options": [
                             {"label": "Minimal", "description": "A small change."},
                             {"label": "Complete", "description": "The full change."}]}]}})
            if "multiquestion" in text.lower():
                second_id = "second-question-" + turn_id
                questions[second_id] = (thread_id, turn_id)
                send({"id": second_id, "method": "item/tool/requestUserInput",
                      "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "second-question-item",
                                 "isBlocking": True, "questions": [{"id": "followup", "header": "Tests",
                                 "question": "Which tests should I run?", "options": None}]}})
        elif "slow" not in text.lower():
            status = "failed" if "fail" in text.lower() else "completed"
            after(0.1, lambda tid=thread_id, uid=turn_id, st=status: finish(tid, uid, st))
    elif method == "turn/steer":
        turn_id = params["expectedTurnId"]
        if turns.get(turn_id, {}).get("status") != "inProgress":
            send({"id": request["id"], "error": {"code": -32602, "message": "Turn no longer active"}})
        else:
            reply(request, {"turnId": turn_id})
    elif method == "turn/interrupt":
        reply(request, {})
        finish(params["threadId"], params["turnId"], "interrupted")
    elif method == "test/echo":
        delay = params.pop("delay", 0)
        after(delay, lambda req=request, p=params: reply(req, p))
    elif method == "test/notify":
        notify("test/notification", params)
        reply(request, {})
    elif method == "test/exit":
        os._exit(0)
    elif method == "test/never":
        continue
    elif method == "test/stderr":
        sys.stderr.write("sensitive-token-do-not-retain" * 10000)
        sys.stderr.flush()
        reply(request, {})
    elif method == "test/oversize":
        with write_lock:
            sys.stdout.write("x" * (4 * 1024 * 1024 + 2) + "\n")
            sys.stdout.flush()
    elif method == "test/malformed":
        with write_lock:
            print("{not json}", flush=True)
    elif method == "test/child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        reply(request, {"pid": child.pid})
    elif method is None and request.get("id") in questions:
        thread_id, turn_id = questions.pop(request["id"])
        if "error" in request:
            finish(thread_id, turn_id, "failed")
        elif (thread_id, turn_id) not in questions.values():
            finish(thread_id, turn_id, text="SPOKEN: Applied your answer.")
    elif method is not None and "id" in request:
        send({"id": request["id"], "error": {"code": -32601, "message": "Unknown method"}})
