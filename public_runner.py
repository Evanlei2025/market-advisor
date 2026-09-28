"""Prepare the public site, then notify after a verified Pages deployment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import public_brief
import public_market
import public_store

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "public_data" / "state.json"
SITE = ROOT / "public_site"
URL = "https://Evanlei2025.github.io/market-advisor/"
TZ = ZoneInfo("Asia/Shanghai")


def _candidate_events(now, before, older=None):
    # Opportunity entry needs two successive dates; exit and overheating notify immediately.
    events = []
    if not before:
        return events
    for name, item in now["sectors"].items():
        prev = before["sectors"].get(name)
        if not prev or prev["state"] == "资料不足" or item["state"] == "资料不足":
            continue
        old = (older or {}).get("sectors", {}).get(name)
        if item["state"] == "优先研究":
            if (prev["state"] != "优先研究" or old is None
                    or old["state"] == "优先研究"):
                continue
            before_state = old["state"] if old else "等待确认"
        elif prev["state"] != item["state"] and (prev["state"] == "优先研究" or item["state"] == "暂缓追涨"):
            before_state = prev["state"]
        else:
            continue
        events.append({"id": f"{now['asof']}:{name}:{before_state}:{item['state']}",
                       "entity": name, "before": before_state, "after": item["state"],
                       "relative20_pp": item["relative20_pp"]})
    return events


def _recently_notified(store, entity, asof):
    from datetime import date
    current = date.fromisoformat(asof)
    return any(0 <= (current - date.fromisoformat(record["asof"])).days < 7
               and any(event["entity"] == entity for event in record.get("events", []))
               for record in store["alerts"])


def prepare(store, research, weekly_due=False):
    import copy
    store = copy.deepcopy(store)
    previous = store["snapshots"][-1] if store["snapshots"] else None
    asof = research["asof"]
    if not previous or previous["asof"] != asof:
        events = _candidate_events(research, previous,
                                   store["snapshots"][-2] if len(store["snapshots"]) >= 2 else None)
        events = [event for event in events if not _recently_notified(store, event["entity"], asof)]
        store["snapshots"].append(research)
        store["snapshots"] = store["snapshots"][-65:]
        if events:
            key = "alert:" + asof
            text = public_brief.alert(research, events)
            store["pending"][key] = {"body": text, "url": URL + "alerts/" + asof + ".html",
                                     "recipients": {}, "kind": "alert"}
            store["alerts"].append({"asof": asof, "body": text, "events": events})
            store["alerts"] = store["alerts"][-20:]
        # Set the reference price on the first observation after publication.
        for entry in store["observations"]:
            current = research["sectors"].get(entry["entity"])
            if (current and "reference_close" not in entry and
                    current["data"]["date"] > entry["asof"]):
                entry["reference_close"] = current["data"]["close"]
                entry["reference_date"] = current["data"]["date"]
                entry["reference_benchmark"] = research["assets"][public_market.BENCHMARK]["close"]
                entry["path"] = [{"date": current["data"]["date"],
                                  "close": current["data"]["close"],
                                  "benchmark": research["assets"][public_market.BENCHMARK]["close"]}]
            elif current and entry.get("reference_close") and current["data"]["date"] > entry["reference_date"]:
                entry.setdefault("path", []).append({
                    "date": current["data"]["date"], "close": current["data"]["close"],
                    "benchmark": research["assets"][public_market.BENCHMARK]["close"]})
                entry["path"] = entry["path"][:60]
    if weekly_due:
        wk = public_brief.week_id(research["date"])
        if wk not in store["weeks"]:
            earlier_wk = max((key for key in store["weeks"] if key < wk), default=None)
            earlier_asof = store["week_asof"].get(earlier_wk) if earlier_wk else None
            earlier = next((s for s in reversed(store["snapshots"])
                            if s["asof"] == earlier_asof), None)
            body = public_brief.weekly(research, earlier, store["observations"])
            store["weeks"][wk] = body
            store["week_asof"][wk] = research["asof"]
            store["pending"]["weekly:" + wk] = {"body": brief_message(research, earlier, wk),
                                                 "url": URL + "weeks/" + wk + ".html",
                                                 "recipients": {}, "kind": "weekly"}
            for name, item in public_brief.highlighted(research):
                store["observations"].append({"entity": name, "asof": item["data"]["date"],
                                               "week": wk})
            store["observations"] = store["observations"][-150:]
            store["weeks"] = dict(sorted(store["weeks"].items())[-104:])
            store["week_asof"] = {k: store["week_asof"][k] for k in store["weeks"]}
    return store


def brief_message(research, previous, wk):
    picks = public_brief.highlighted(research)
    lines = [f"市场机会与风险周报 {wk}｜截至{research['asof']}",
             "较上期：" + public_brief.recap(research, previous, [])[0],
             "宽基：" + "、".join(f"{name}近5日{item['returns']['5']:+.1f}%"
                              for name, item in research["assets"].items()),
             "行业关注：" + ("、".join(name for name, _ in picks) if picks else "暂无同时满足条件的方向"),
             "判断边界：行业研究筛选不等于确定买点；详见证据与反证。"]
    return "\n".join(lines)


def prepare_run(state_path=STATE, output=SITE, today=None, weekly_due=None, collector=None):
    today = today or datetime.now(TZ).date()
    state = public_store.load(state_path)
    weekly_due = (today.weekday() == 5 or not state["weeks"]) if weekly_due is None else weekly_due
    # A Saturday digest uses the most recent complete daily observation.
    if weekly_due and today.weekday() >= 5 and state["snapshots"]:
        latest = state["snapshots"][-1]
        from main import DataFetcher
        fetcher = DataFetcher()
        expected = public_market.expected_session(today, fetcher, fetcher.ak)
        if latest["asof"] == expected:
            research = dict(latest, date=today.isoformat())
        else:
            raise ValueError(f"周报快照截至{latest['asof']}，最近交易日应为{expected}")
    else:
        raw = (collector or public_market.collect)(today=today)
        research = public_market.research(raw)
    if weekly_due and public_brief.week_id(research["date"]) not in state["weeks"]:
        import public_ai
        research["editor_note"] = public_ai.explain(research)
    if weekly_due and today.weekday() >= 5 and research["asof"] == today.isoformat():
        raise ValueError("weekly edition cannot use a weekend market date")
    if collector is None:
        from main import DataFetcher
        fetcher = DataFetcher()
        expected = public_market.expected_session(today, fetcher, fetcher.ak)
        if research["asof"] != expected:
            raise ValueError(f"行情日期{research['asof']}未对齐最近交易日{expected}")
    updated = prepare(state, research, weekly_due)
    if not updated["weeks"]:
        raise ValueError("no public weekly page to deploy")
    public_brief.render_site(output, updated["weeks"], updated["alerts"])
    public_store.save(state_path, updated)
    return updated


def recipients_from_config(config):
    recipients = config.get("recipients")
    if not isinstance(recipients, list):
        # One-release migration bridge: accept the old secret in memory only.
        # No client name, endpoint, or legacy config is written to public state.
        recipients = []
        global_push = config.get("push", {})
        if isinstance(global_push, dict):
            recipients.append({"id": "legacy-global", "channel": global_push.get("channel"),
                               "key": global_push.get("serverchan_key"),
                               "webhook": global_push.get("wecom_webhook")})
        for client in config.get("clients", {}).values():
            if isinstance(client, dict) and isinstance(client.get("push"), dict):
                push = client["push"]
                recipients.append({"id": "legacy-channel", "channel": push.get("channel"),
                                   "key": push.get("serverchan_key"),
                                   "webhook": push.get("wecom_webhook")})
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("no public delivery recipients configured")
    out = {}
    endpoints = set()
    for item in recipients:
        alias = str(item.get("id", ""))
        channel = item.get("channel")
        endpoint = item.get("key") if channel == "serverchan" else item.get("webhook")
        if not alias or not alias.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid recipient alias")
        if channel not in ("serverchan", "wecom") or not endpoint:
            raise ValueError("invalid recipient channel")
        if (channel, endpoint) in endpoints:
            raise ValueError("duplicate recipient or destination")
        # Only anonymous IDs go into the public Git ledger, never customer names.
        public_id = "receiver-" + hashlib.sha256((channel + ":" + endpoint).encode()).hexdigest()[:16]
        out[public_id] = {"channel": channel, "endpoint": endpoint}
        endpoints.add((channel, endpoint))
    return out


def send(recipient, title, text, transport=requests.post):
    if recipient["channel"] == "serverchan":
        url = "https://sctapi.ftqq.com/" + recipient["endpoint"] + ".send"
        payload = {"title": title, "desp": text}
    else:
        url = recipient["endpoint"]
        payload = {"msgtype": "markdown", "markdown": {"content": "### " + title + "\n" + text}}
    response = transport(url, json=payload, timeout=15)
    response.raise_for_status()
    result = response.json()
    ok = result.get("code") == 0 if recipient["channel"] == "serverchan" else result.get("errcode") == 0
    if not ok:
        raise RuntimeError("provider rejected delivery")


def _delivery_recipients(delivery_json):
    source = delivery_json or os.environ.get("ADVISOR_DELIVERY_JSON", "")
    if not source:
        raise ValueError("ADVISOR_DELIVERY_JSON missing; refusing to read client configuration")
    return recipients_from_config(json.loads(source))


def mark_deployed(state_path=STATE, persist=public_store.save):
    state = public_store.load(state_path)
    if not state["weeks"]:
        raise ValueError("no weekly edition to mark deployed")
    wk = max(state["weeks"])
    state["deployed"][wk] = state["week_asof"][wk]
    persist(state_path, state)


def eligible_events(alias, state_path=STATE, delivery_json=None):
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients:
        raise ValueError("invalid recipient alias")
    state = public_store.load(state_path)
    return [event_id for event_id, event in state["pending"].items()
            if alias not in event["recipients"]]


def reserve_run(alias, attempt, event_id, state_path=STATE, delivery_json=None, persist=public_store.save):
    state = public_store.load(state_path)
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients or not attempt or event_id not in state["pending"]:
        raise ValueError("invalid delivery attempt")
    event = state["pending"][event_id]
    if alias in event["recipients"]:
        raise ValueError("delivery already reserved")
    content = event["body"] + "\n" + event["url"]
    if len(content.encode("utf-8")) > 3500:
        raise ValueError("public push exceeds byte limit")
    event["recipients"][alias] = {"status": "unknown", "attempt": attempt}
    persist(state_path, state)


def notify_run(alias, attempt, event_id, state_path=STATE, delivery_json=None, sender=send,
               persist=public_store.save):
    state = public_store.load(state_path)
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients or not attempt or event_id not in state["pending"]:
        raise ValueError("invalid delivery attempt")
    event = state["pending"][event_id]
    if event["recipients"].get(alias) != {"status": "unknown", "attempt": attempt}:
        raise ValueError("no reserved delivery for this attempt")
    content = event["body"] + "\n" + event["url"]
    try:
        sender(recipients[alias], "市场周报" if event["kind"] == "weekly" else "市场变化", content)
    except Exception:
        raise RuntimeError("delivery status unknown for " + alias) from None
    event["recipients"][alias] = {"status": "accepted", "attempt": attempt}
    if all(event["recipients"].get(name, {}).get("status") == "accepted" for name in recipients):
        state["delivered"][event_id] = datetime.now(TZ).isoformat()
        del state["pending"][event_id]
    state["delivered"] = dict(sorted(state["delivered"].items())[-300:])
    persist(state_path, state)


def reconcile_run(alias, event_id, status, state_path=STATE, delivery_json=None,
                  persist=public_store.save):
    """Manually resolve a provider-unknown notification before resuming automation."""
    state = public_store.load(state_path)
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients or event_id not in state["pending"]:
        raise ValueError("invalid reconciliation target")
    if status not in ("accepted", "retry"):
        raise ValueError("status must be accepted or retry")
    event = state["pending"][event_id]
    if status == "accepted":
        event["recipients"][alias] = {"status": "accepted", "reconciled": True}
    else:
        event["recipients"].pop(alias, None)
    if status == "accepted" and all(event["recipients"].get(name, {}).get("status") == "accepted" for name in recipients):
        state["delivered"][event_id] = datetime.now(TZ).isoformat()
        del state["pending"][event_id]
    state["delivered"] = dict(sorted(state["delivered"].items())[-300:])
    persist(state_path, state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", nargs="?", default="prepare",
                        choices=["prepare", "deployed", "recipients", "events", "reserve", "notify", "reconcile"])
    parser.add_argument("--weekly", action="store_true", help="Publish an edition outside the Saturday schedule")
    parser.add_argument("--recipient")
    parser.add_argument("--attempt")
    parser.add_argument("--event")
    parser.add_argument("--status")
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare_run(weekly_due=True if args.weekly else None)
    elif args.phase == "deployed":
        mark_deployed()
    elif args.phase == "recipients":
        for alias in _delivery_recipients(None):
            print(alias)
    elif args.phase == "events":
        for event_id in eligible_events(args.recipient):
            print(event_id)
    elif args.phase == "reserve":
        reserve_run(args.recipient, args.attempt, args.event)
    elif args.phase == "reconcile":
        reconcile_run(args.recipient, args.event, args.status)
    else:
        notify_run(args.recipient, args.attempt, args.event)


if __name__ == "__main__":
    main()
