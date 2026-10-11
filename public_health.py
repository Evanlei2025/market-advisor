"""Daily publication watchdog: ping only while the public service is current."""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

import public_brief
import public_market
import public_runner
import public_store


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
    if not state["snapshots"] or state["snapshots"][-1]["asof"] != expected:
        raise ValueError(f"missing market snapshot for {expected}")
    wk = due_week(now)
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


def main():
    url = os.environ.get("PUBLIC_HEARTBEAT_URL", "")
    if not url.startswith("https://"):
        raise ValueError("PUBLIC_HEARTBEAT_URL missing or invalid")
    now = datetime.now(TZ)
    from public_sources import DataFetcher
    fetcher = DataFetcher()
    expected = public_market.expected_session(now.date(), fetcher)
    assert_current(public_store.load(public_runner.STATE), now, expected)
    response = requests.get(url, timeout=15)
    response.raise_for_status()


if __name__ == "__main__":
    main()
