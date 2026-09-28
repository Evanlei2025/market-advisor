"""Daily publication watchdog: ping only while the public service is current."""
from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

import public_brief
import public_market
import public_runner
import public_store


def assert_current(state, today, expected):
    if not state["snapshots"] or state["snapshots"][-1]["asof"] != expected:
        raise ValueError(f"missing market snapshot for {expected}")
    # Allow the new weekly edition until the following week's first publication window.
    if today.weekday() >= 5:
        wk = public_brief.week_id(today.isoformat())
        if wk not in state["weeks"]:
            raise ValueError("missing weekly edition " + wk)
    latest_week = max(state["weeks"], default=None)
    if latest_week and (not state["week_asof"].get(latest_week)
                        or state["deployed"].get(latest_week) != state["week_asof"][latest_week]):
        raise ValueError("latest weekly edition was not deployed")
    if any(item.get("kind") == "weekly" and item.get("recipients") for item in state["pending"].values()):
        raise ValueError("weekly delivery incomplete")


def main():
    url = os.environ.get("PUBLIC_HEARTBEAT_URL", "")
    if not url.startswith("https://"):
        raise ValueError("PUBLIC_HEARTBEAT_URL missing or invalid")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    from main import DataFetcher
    fetcher = DataFetcher()
    expected = public_market.expected_session(today, fetcher, fetcher.ak)
    assert_current(public_store.load(public_runner.STATE), today, expected)
    response = requests.get(url, timeout=15)
    response.raise_for_status()


if __name__ == "__main__":
    main()
