# -*- coding: utf-8 -*-
"""Bounded worker wrapper for AkShare calls that lack request timeouts."""
import threading


def call_with_timeout(fn, timeout=90, *args, **kwargs):
    """Run a network call on a daemon thread and raise after its time budget."""
    out = {}

    def _run():
        try:
            out["r"] = fn(*args, **kwargs)
        except Exception as e:
            out["e"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("call %s timeout>%ss" % (getattr(fn, "__name__", "?"), timeout))
    if "e" in out:
        raise out["e"]
    return out["r"]
