# -*- coding: utf-8 -*-
"""akshare 调用统一超时保护（ak 内部 requests 无 timeout，网络异常时可能永久挂起）。

纯 stdlib 线程包装：超时抛 TimeoutError，由调用方（fetch_section 等）重试或降级。
main/rules/state_store 共用（aktime 无任何依赖，避免循环 import）。
"""
import threading


def call_with_timeout(fn, timeout=90, *args, **kwargs):
    """在独立 daemon 线程中执行 fn(*args, **kwargs)，超过 timeout 秒抛 TimeoutError。
    成功原样返回；异常原样传播（含 TimeoutError）。"""
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
