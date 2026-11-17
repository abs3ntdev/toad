"""A tiny ACP agent for swarm tests: many sessions, echoes the prompt back."""

import json
import sys
import threading
import time

lock = threading.Lock()
sessions = 0


def send(message):
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def prompt(request):
    params = request["params"]
    sid = params["sessionId"]
    text = params["prompt"][0]["text"]
    delay = 0.3 if "slow" in text else 0.01
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": sid, "update": {"sessionUpdate": "plan", "entries": [
            {"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]}}})
    time.sleep(delay)
    last = text.strip().splitlines()[-1]
    for chunk in ("done: ", last):
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": chunk}}}})
    if "fail" in last:
        send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -1, "message": "boom"}})
    else:
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": "end_turn"}})


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif method == "session/new":
        sessions += 1
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": f"s{sessions}"}})
    elif method == "session/prompt":
        threading.Thread(target=prompt, args=(request,), daemon=True).start()
