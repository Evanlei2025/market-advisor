"""Detailed operator alerts without using subscribed delivery channels."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def alert_details(context=None, steps=None, state_path="public_data/state.json", env=None):
    env = env if env is not None else os.environ
    context = context or {}
    steps = steps or {}
    failed = [name for name, result in steps.items() if result.get("outcome") == "failure"]
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        latest = state["snapshots"][-1]["asof"] if state["snapshots"] else "none"
    except (OSError, ValueError, KeyError, TypeError):
        latest = "unavailable"
    workflow = env.get("GITHUB_WORKFLOW", "local")
    run_id = env.get("GITHUB_RUN_ID", "local")
    link = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY', 'Evanlei2025/market-advisor')}/actions/runs/{run_id}"
    lines = [f"workflow: {workflow}", f"run ID: {run_id}", f"run: {link}",
             f"failed stage: {', '.join(failed) or 'none (scheduler delay warning)'}",
             f"logical run date: {context.get('logical_date', 'unresolved')}",
             f"task kind: {context.get('kind', 'unresolved')}", f"latest market asof: {latest}",
             f"observed startup: {context.get('observed_at', 'unresolved')}",
             f"inferred scheduled slot: {context.get('scheduled_for', 'unresolved')}",
             f"scheduler delay: {context.get('delay_seconds', 'unresolved')} seconds"]
    if context.get("delay_exceeds_grace"):
        lines.append("Schedule delay exceeded the 2-hour grace; classify separately from data-source failure.")
    return "\n\n".join(lines)


def notify(title, text, key=None, opener=urllib.request.urlopen):
    key = key if key is not None else os.environ.get("SC_KEY", "")
    if not key:
        raise ValueError("operator alert key missing")
    request = urllib.request.Request("https://sctapi.ftqq.com/" + key + ".send",
                                     data=json.dumps({"title": title, "desp": text}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=15) as response:
            result = json.loads(response.read())
        if result.get("code") != 0:
            raise ValueError("operator alert provider rejected request")
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("operator alert delivery failed; check provider and Actions run") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", action="store_true")
    parser.add_argument("--context", default=os.environ.get("PUBLIC_CONTEXT_FILE", ""))
    args = parser.parse_args()
    try:
        context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        context = {}
    if args.delay and not context.get("delay_exceeds_grace"):
        return
    title = "市场简报任务延迟超过宽限" if args.delay else "市场简报任务失败"
    details = alert_details(context, json.loads(os.environ.get("PUBLIC_STEPS_JSON", "{}")))
    print(details)
    notify(title, details)


if __name__ == "__main__":
    main()
