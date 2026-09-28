"""Optional bounded explanation of an already approved public research brief."""
import json
import os
import re

import requests


def explain(research, transport=requests.post):
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    picks = [(name, item) for name, item in research["sectors"].items()
             if item["state"] == "优先研究"][:3]
    if not picks:
        return None
    evidence = [{"industry": name, "relative20_pp": item["relative20_pp"],
                 "participation": item["data"].get("participation"), "asof": item["data"]["date"]}
                for name, item in picks]
    prompt = ("根据以下已审核的公开行业观察，写一句不超过60字的总括。只能用所给行业名，"
              "不能增添数字、事件、买卖指令、预测或因果断言。只输出 JSON {\"note\":\"...\"}。\n"
              + json.dumps(evidence, ensure_ascii=False))
    try:
        r = transport("https://api.deepseek.com/chat/completions",
                      headers={"Authorization": "Bearer " + key},
                      json={"model": "deepseek-chat", "messages": [
                          {"role": "system", "content": "你是公开市场资料的编辑。严格限定事实，不增补任何信息。"},
                          {"role": "user", "content": prompt}], "temperature": 0.1,
                            "max_tokens": 120, "response_format": {"type": "json_object"}}, timeout=25)
        r.raise_for_status()
        note = json.loads(r.json()["choices"][0]["message"]["content"])["note"].strip()
        if (not note or len(note) > 60 or re.search(r"[0-9０-９]|买入|卖出|收益|预计|预测|一定|必然|上涨|下跌", note)):
            return None
        # Any industry mentioned must already be in the approved shortlist.
        universe = set(research["sectors"])
        approved = {name for name, _ in picks}
        if any(name in note for name in universe - approved):
            return None
        return note
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError):
        return None
