#!/usr/bin/env python3
import json
import time
import sys

payload = json.load(sys.stdin)
request = payload.get("latest_request", "")
if "need input" in request.lower():
    result = {
        "status": "needs_input",
        "summary": "Blocked on missing input",
        "details": "I cannot continue without the requested missing value.",
        "questions": ["What value should I use?"],
        "artifacts": [],
    }
elif "fail" in request.lower():
    result = {
        "status": "failed",
        "summary": "Worker failed",
        "details": "Synthetic failure from test worker.",
        "questions": [],
        "artifacts": [],
    }
elif "sleep" in request.lower() or "slow" in request.lower():
    time.sleep(30)
    result = {
        "status": "completed",
        "summary": "Slept",
        "details": f"Finished slow task: {request}",
        "questions": [],
        "artifacts": [],
    }
else:
    result = {
        "status": "completed",
        "summary": "Done",
        "details": f"Processed: {request}",
        "questions": [],
        "artifacts": [payload.get("thread_id", "")],
    }
print(json.dumps(result))
