"""Failure/recovery exercise restricted to a separate external test check."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path

import requests

import public_health
import public_ops
import public_runner
import public_store


def test_url():
    url = os.environ.get("PUBLIC_DRILL_HEARTBEAT_URL", "")
    if not url.startswith("https://hc-ping.com/") or len(url.removeprefix("https://hc-ping.com/")) != 36:
        raise ValueError("separate test Healthchecks URL missing or invalid")
    if url == os.environ.get("PUBLIC_HEARTBEAT_URL"):
        raise ValueError("drill must use a separate check")
    return url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["seed", "inject", "alert", "recover"])
    args = parser.parse_args()
    url = test_url()
    context = json.loads(Path(os.environ["PUBLIC_CONTEXT_FILE"]).read_text(encoding="utf-8"))
    if args.phase == "inject":
        # Modify only a private copy in memory; the production file is read-only.
        state = copy.deepcopy(public_store.load(public_runner.STATE))
        state["snapshots"] = []
        public_health.assert_current(state, datetime.now(public_runner.TZ), context["logical_date"])
        raise RuntimeError("failure injection did not trigger the watchdog")
    if args.phase == "recover":
        public_health.check_run(url=url, context=context)
        print("Independent test check recovered after real production-state validation")
        return
    if args.phase == "alert":
        details = "INDEPENDENT TEST DRILL\n\n" + public_ops.alert_details(
            context, {"inject": {"outcome": "failure"}})
        response = requests.post(url + "/fail", data=details.encode("utf-8"), timeout=15)
    else:
        response = requests.get(url, timeout=15)
    response.raise_for_status()
    print("Independent test check " + args.phase + " accepted")


if __name__ == "__main__":
    main()
