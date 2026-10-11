"""Durable, public-only publication and delivery state."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

import public_brief

SCHEMA = 3


def empty_state():
    return {"schema": SCHEMA, "snapshots": [], "weeks": {}, "week_asof": {},
            "deployed": {}, "prepared_pages": {}, "deployed_pages": {}, "observations": [],
            "pending": {}, "delivered": {}, "alerts": []}


def _date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid public state date")
    return value


def _timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("invalid public state timestamp")


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("invalid page version")


def _positive(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid public state price")


def migrate(data):
    """Preserve historical evidence without consulting the current renderer."""
    if (not isinstance(data, dict) or type(data.get("schema")) is not int
            or data["schema"] not in (1, 2, SCHEMA)):
        raise ValueError("invalid public state; refusing silent baseline reset")
    data = copy.deepcopy(data)
    for key in ("snapshots", "observations", "alerts"):
        if not isinstance(data.get(key), list):
            raise ValueError("invalid " + key)
    for key in ("weeks", "pending", "delivered", "deployed"):
        if not isinstance(data.get(key), dict):
            raise ValueError("invalid " + key)
    if any(not isinstance(entry, dict) for entry in data["observations"]):
        raise ValueError("invalid observation")
    if any(not isinstance(event, dict) for event in data["pending"].values()):
        raise ValueError("invalid pending event")
    if data["schema"] == 2:
        if (not isinstance(data.get("deployed_pages"), dict)
                or any(not isinstance(record, dict) for record in data["deployed_pages"].values())):
            raise ValueError("invalid deployed_pages")
    if data["schema"] == 1:
        data.setdefault("week_asof", {})
        if not isinstance(data["week_asof"], dict):
            raise ValueError("invalid week_asof")
        if data.get("weeks") and not data["week_asof"]:
            for wk in data["weeks"]:
                candidates = [snapshot for snapshot in data.get("snapshots", [])
                              if isinstance(snapshot, dict)
                              and public_brief.week_id(snapshot.get("date", "")) == wk]
                if candidates:
                    data["week_asof"][wk] = candidates[-1].get("asof")
        data["legacy_deployed"] = data.get("deployed", {})
        data["deployed"] = {}
        data["deployed_pages"] = {}
        data["prepared_pages"] = {}
        for entry in data.get("observations", []):
            if not entry.get("published_at"):
                legacy = {key: entry.pop(key) for key in
                          ("reference_close", "reference_date", "reference_benchmark", "path")
                          if key in entry}
                if legacy:
                    entry["legacy_returns"] = legacy
        for event in data.get("pending", {}).values():
            if event.get("page_version") is not None:
                event["legacy_page_version"] = event["page_version"]
            event["page_version"] = None
    elif data["schema"] == 2:
        prepared = {key: record.get("page_version")
                    for key, record in data.get("deployed_pages", {}).items()}
        prepared.update({key: event.get("page_version")
                         for key, event in data.get("pending", {}).items()})
        data["prepared_pages"] = prepared
    data["schema"] = SCHEMA
    return data


def validate(data):
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA:
        raise ValueError("invalid public state schema")
    for key in ("snapshots", "observations", "alerts"):
        if not isinstance(data.get(key), list):
            raise ValueError("invalid " + key)
    for key in ("weeks", "week_asof", "prepared_pages", "deployed", "deployed_pages", "pending", "delivered"):
        if not isinstance(data.get(key), dict):
            raise ValueError("invalid " + key)
    if set(data["weeks"]) != set(data["week_asof"]):
        raise ValueError("weeks and week_asof differ")
    for wk, body in data["weeks"].items():
        public_brief.valid_week(wk)
        _date(data["week_asof"][wk])
        if not isinstance(body, str) or not body:
            raise ValueError("invalid weekly body")
        start = date.fromisocalendar(int(wk[:4]), int(wk[-2:]), 1)
        if date.fromisoformat(data["week_asof"][wk]) > start.fromordinal(start.toordinal() + 6):
            raise ValueError("weekly asof after edition week")
    last_asof = None
    for snapshot in data["snapshots"]:
        if not isinstance(snapshot, dict):
            raise ValueError("invalid snapshot")
        _date(snapshot.get("date"))
        asof = _date(snapshot.get("asof"))
        if asof > snapshot["date"] or (last_asof and asof <= last_asof):
            raise ValueError("invalid snapshot asof ordering")
        last_asof = asof
        if not isinstance(snapshot.get("assets"), dict) or "沪深300" not in snapshot["assets"]:
            raise ValueError("invalid snapshot assets")
        if not isinstance(snapshot.get("sectors"), dict) or not isinstance(snapshot.get("quality"), list):
            raise ValueError("invalid snapshot sectors or quality")
        if any(not isinstance(item, dict) or not isinstance(item.get("data"), dict)
               for item in snapshot["sectors"].values()):
            raise ValueError("invalid snapshot sector data")
        prices = list(snapshot["assets"].values()) + [item["data"] for item in snapshot["sectors"].values()]
        for item in prices:
            if not isinstance(item, dict):
                raise ValueError("invalid snapshot price")
            if _date(item.get("date")) != asof:
                raise ValueError("snapshot price date differs from asof")
            _positive(item.get("close"))
    page_ids = {"weekly:" + wk for wk in data["weeks"]}
    for alert in data["alerts"]:
        if not isinstance(alert, dict) or not isinstance(alert.get("body"), str) or not alert["body"]:
            raise ValueError("invalid alert page")
        event_id = "alert:" + _date(alert.get("asof"))
        if event_id in page_ids:
            raise ValueError("duplicate alert page")
        page_ids.add(event_id)
    for event_id, version in data["prepared_pages"].items():
        if event_id not in page_ids:
            raise ValueError("prepared event has no page")
        _hash(version)
    for wk, asof in data["deployed"].items():
        if wk not in data["weeks"] or data["week_asof"][wk] != asof:
            raise ValueError("deployed and week_asof differ")
        if "weekly:" + wk not in data["deployed_pages"]:
            raise ValueError("deployed weekly page version missing")
    for event_id, record in data["deployed_pages"].items():
        if event_id not in page_ids or not isinstance(record, dict):
            raise ValueError("invalid deployed page")
        _hash(record.get("page_version"))
        _timestamp(record.get("published_at"))
        if not isinstance(record.get("deployment_id"), str) or not record["deployment_id"]:
            raise ValueError("invalid deployment id")
    for event_id, event in data["pending"].items():
        if not isinstance(event, dict) or event_id not in page_ids:
            raise ValueError("pending event has no page")
        kind = event.get("kind")
        if kind not in ("weekly", "alert") or not event_id.startswith(kind + ":"):
            raise ValueError("invalid pending event kind")
        if not isinstance(event.get("body"), str) or not event["body"]:
            raise ValueError("invalid pending event body")
        if not isinstance(event.get("url"), str) or not event["url"].startswith("https://"):
            raise ValueError("invalid pending event url")
        if event.get("page_version") is not None:
            _hash(event["page_version"])
        if event.get("page_version") != data["prepared_pages"].get(event_id):
            raise ValueError("pending event page version differs")
        if not isinstance(event.get("recipients"), dict):
            raise ValueError("invalid pending recipients")
        for alias, record in event["recipients"].items():
            if not isinstance(alias, str) or not re.fullmatch(r"receiver-[a-f0-9]{16}", alias):
                raise ValueError("invalid recipient alias")
            if not isinstance(record, dict) or record.get("status") not in ("reserved", "unknown", "accepted"):
                raise ValueError("invalid recipient status")
            if not isinstance(record.get("attempt"), str) or not record["attempt"]:
                if record.get("status") != "accepted" or record.get("reconciled") is not True:
                    raise ValueError("recipient attempt missing")
        retired = event.get("retired_attempts", {})
        if not isinstance(retired, dict):
            raise ValueError("invalid retired attempts")
        for alias, attempts in retired.items():
            if (not isinstance(alias, str) or not re.fullmatch(r"receiver-[a-f0-9]{16}", alias)
                    or not isinstance(attempts, list)
                    or any(not isinstance(attempt, str) or not attempt for attempt in attempts)):
                raise ValueError("invalid retired attempts")
    if set(data["pending"]) & set(data["delivered"]):
        raise ValueError("event is both pending and delivered")
    for event_id, timestamp in data["delivered"].items():
        if not isinstance(event_id, str) or ":" not in event_id:
            raise ValueError("invalid delivered event id")
        kind, identity = event_id.split(":", 1)
        if kind == "weekly":
            public_brief.valid_week(identity)
        elif kind == "alert":
            _date(identity)
        else:
            raise ValueError("invalid delivered event kind")
        _timestamp(timestamp)
    for entry in data["observations"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("entity"), str):
            raise ValueError("invalid observation")
        public_brief.valid_week(entry.get("week"))
        _date(entry.get("asof"))
        if entry.get("published_at"):
            _timestamp(entry["published_at"])
            if not isinstance(entry.get("deployment_id"), str) or not entry["deployment_id"]:
                raise ValueError("observation deployment missing")
            if _date(entry.get("published_asof")) < entry["asof"]:
                raise ValueError("observation publication precedes asof")
        elif any(entry.get(key) is not None for key in ("reference_close", "reference_date", "path", "deployment_id")):
            raise ValueError("unpublished observation has returns")
        path = entry.get("path", [])
        if not isinstance(path, list) or len(path) > 61:
            raise ValueError("invalid observation path")
        if path:
            _positive(entry.get("reference_close"))
            _positive(entry.get("reference_benchmark"))
            if _date(entry.get("reference_date")) <= entry["published_asof"]:
                raise ValueError("reference precedes publication")
            if path[0] != {"date": entry["reference_date"], "close": entry["reference_close"],
                           "benchmark": entry["reference_benchmark"]}:
                raise ValueError("reference differs from observation path")
            previous = None
            for point in path:
                if not isinstance(point, dict):
                    raise ValueError("invalid observation path point")
                day = _date(point.get("date"))
                _positive(point.get("close"))
                _positive(point.get("benchmark"))
                if previous and day <= previous:
                    raise ValueError("observation path dates are not increasing")
                previous = day
    return data


def load(path):
    path = Path(path)
    if not path.exists():
        return empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate(migrate(data))


def save(path, data):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError("public state directory missing")
    validate(data)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".public-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
