"""Daily publication watchdog: ping only while the public service is current."""
from __future__ import annotations

import os
import argparse
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

import public_brief
import public_market
import public_runner
import public_store
import public_schedule


TZ = ZoneInfo("Asia/Shanghai")
WEEKLY_SCHEDULE = time(17, 35)
WEEKLY_GRACE = timedelta(hours=2)


def due_week(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("timezone-aware heartbeat datetime required")
    now = now.astimezone(TZ)
    saturday = now.date() - timedelta(days=(now.weekday() - 5) % 7)
    deadline = datetime.combine(saturday, WEEKLY_SCHEDULE, TZ) + WEEKLY_GRACE
    if now < deadline:
        saturday -= timedelta(days=7)
    return public_brief.week_id(saturday.isoformat())


def assert_current(state, now, expected):
    wk = due_week(now)
    if not state["snapshots"] or state["snapshots"][-1]["asof"] != expected:
        latest = state["snapshots"][-1]["asof"] if state["snapshots"] else "none"
        raise ValueError(f"missing market snapshot for {expected}; latest {latest}; due weekly edition {wk}")
    if wk not in state["weeks"]:
        raise ValueError("missing weekly edition " + wk)
    for edition in {wk, max(state["weeks"])}:
        event_id = "weekly:" + edition
        prepared = state["prepared_pages"].get(event_id)
        if (not state["week_asof"].get(edition)
                or state["deployed"].get(edition) != state["week_asof"][edition]
                or not prepared
                or state["deployed_pages"].get(event_id, {}).get("page_version") != prepared):
            raise ValueError("weekly edition was not deployed at prepared version: " + edition)
    if state["pending"]:
        unresolved = [event_id for event_id, event in state["pending"].items()
                      if any(item.get("status") == "unknown" for item in event["recipients"].values())]
        if unresolved:
            raise ValueError("delivery status unknown: " + ", ".join(unresolved))
        raise ValueError("delivery incomplete: " + ", ".join(state["pending"]))


def check_run(state_path=public_runner.STATE, url=None, now=None, context=None,
              fetcher=None, transport=requests.get):
    url = url if url is not None else os.environ.get("PUBLIC_HEARTBEAT_URL", "")
    if not url.startswith("https://"):
        raise ValueError("PUBLIC_HEARTBEAT_URL missing or invalid")
    now = now or datetime.now(TZ)
    context = context or public_schedule.resolve(now=now, heartbeat=True)
    logical_date = public_schedule.parse_date(context["logical_date"])
    from public_sources import DataFetcher
    fetcher = fetcher or DataFetcher()
    expected = public_market.expected_session(logical_date, fetcher)
    assert_current(public_store.load(state_path), now, expected)
    response = transport(url, timeout=15)
    response.raise_for_status()
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logical-date")
    parser.add_argument("--context", default=os.environ.get("PUBLIC_CONTEXT_FILE"))
    args = parser.parse_args()
    context = (json.loads(Path(args.context).read_text(encoding="utf-8")) if args.context else
               public_schedule.resolve(logical_date=args.logical_date, heartbeat=True))
    print("Verified market snapshot " + check_run(context=context))


if __name__ == "__main__":
    main()
