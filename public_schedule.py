"""Logical Shanghai run dates and scheduler telemetry (standard library only)."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DAILY_TIME = time(19, 35)
WEEKLY_TIME = time(17, 35)
DAILY_CRON = "35 11 * * 1-5"
WEEKLY_CRON = "35 9 * * 6"
HEARTBEAT_CRON = "17 14 * * *"
DELAY_GRACE_SECONDS = 2 * 3600


def parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            day = date.fromisoformat(value)
            if day.isoformat() == value:
                return day
        except ValueError:
            pass
    raise ValueError("logical run date must be YYYY-MM-DD")


def completed_day(now):
    return now.date() if now.time() >= DAILY_TIME else now.date() - timedelta(days=1)


def resolve(now=None, kind="auto", logical_date=None, schedule="", heartbeat=False, backfill=False):
    now = now or datetime.now(TZ)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("timezone-aware run datetime required")
    now = now.astimezone(TZ)
    if kind not in ("auto", "daily", "weekly"):
        raise ValueError("run kind must be auto, daily, or weekly")
    if schedule and schedule not in (DAILY_CRON, WEEKLY_CRON, HEARTBEAT_CRON):
        raise ValueError("unrecognized Actions schedule")
    if heartbeat:
        kind = "heartbeat"
    elif schedule:
        kind = "weekly" if schedule == WEEKLY_CRON else "daily"
    elif kind == "auto":
        kind = "weekly" if now.weekday() in (5, 6) else "daily"
    if logical_date:
        day = parse_date(logical_date)
    elif kind == "weekly":
        day = now.date() - timedelta(days=(now.weekday() - 5) % 7)
        if now.weekday() == 5 and now.time() < WEEKLY_TIME:
            day -= timedelta(days=7)
    else:
        day = completed_day(now)
    if day > now.date():
        raise ValueError("logical date is in the future; wait for the scheduled publication window")
    if kind in ("daily", "heartbeat") and day > completed_day(now):
        raise ValueError("logical date is not complete; wait until 19:35 Asia/Shanghai")
    if kind == "weekly" and day.weekday() != 5:
        raise ValueError("weekly logical date must be a Saturday")
    if backfill and not logical_date:
        raise ValueError("backfill requires an explicit historical logical date")
    slot = WEEKLY_TIME if kind == "weekly" else time(22, 17) if heartbeat else DAILY_TIME
    scheduled_for = datetime.combine(day, slot, TZ)
    delay = max(0, int((now - scheduled_for).total_seconds())) if schedule else 0
    # The heartbeat's market cutoff is 19:35, but its expected trigger is 22:17.
    # Between these slots today's snapshot is due even when a previous heartbeat
    # is delayed. Use the last elapsed trigger solely for latency telemetry.
    if heartbeat and schedule and now < scheduled_for:
        scheduled_for -= timedelta(days=1)
        delay = max(0, int((now - scheduled_for).total_seconds()))
    return {"kind": kind, "logical_date": day.isoformat(), "backfill": bool(backfill),
            "observed_at": now.isoformat(), "scheduled_for": scheduled_for.isoformat(),
            "schedule": schedule, "delay_seconds": delay,
            "delay_exceeds_grace": bool(schedule and delay > DELAY_GRACE_SECONDS)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--heartbeat", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    context = resolve(kind=os.environ.get("PUBLIC_RUN_KIND", "auto"),
                      logical_date=os.environ.get("PUBLIC_LOGICAL_DATE") or None,
                      schedule=os.environ.get("PUBLIC_SCHEDULE", ""), heartbeat=args.heartbeat,
                      backfill=os.environ.get("PUBLIC_BACKFILL", "false").lower() == "true")
    Path(args.output).write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(context))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"Logical task: {context['kind']} / {context['logical_date']}\n\n"
                         f"Observed startup delay: {context['delay_seconds']} seconds\n")


if __name__ == "__main__":
    main()
