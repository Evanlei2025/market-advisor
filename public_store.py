"""Durable, public-only publication and delivery state."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def load(path):
    path = Path(path)
    if not path.exists():
        return {"schema": 1, "snapshots": [], "weeks": {}, "week_asof": {}, "deployed": {}, "observations": [],
                "pending": {}, "delivered": {}, "alerts": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("snapshots"), list) or not isinstance(data.get("weeks"), dict):
        raise ValueError("invalid public state; refusing silent baseline reset")
    for key in ("observations", "alerts"):
        if not isinstance(data.get(key), list):
            raise ValueError("invalid " + key)
    for key in ("pending", "delivered", "deployed"):
        if not isinstance(data.get(key), dict):
            raise ValueError("invalid " + key)
    if not isinstance(data.get("week_asof", {}), dict):
        raise ValueError("invalid week_asof")
    data.setdefault("week_asof", {})
    return data


def save(path, data):
    path = Path(path)
    if not path.parent.is_dir():
        raise ValueError("public state directory missing")
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
