"""OTLP/JSON 导出：把内核 trace span 树转换为 OTLP resourceSpans 形状。

零依赖实现（不引入 opentelemetry SDK）：产出符合 OTLP/HTTP JSON 协议的
``resourceSpans`` 结构，可直接 POST 到任何 OTLP/HTTP collector
（Phoenix / Langfuse / Jaeger / OpenTelemetry Collector 均兼容），
或存档后用标准工具回看。

约定映射：
- ExecutionTracer 树 → 一个 resource + 一个 scopeSpan；
  根 span（run_oneshot）= 整次执行，children 递归展平为子 span。
- span kind 统一 INTERNAL(1)；duration_ms → 纳秒字符串。
"""

from __future__ import annotations

import time
from typing import Any


def _sid(seed: Any) -> str:
    """由种子稳定生成 16 位 hex spanId（同输入同输出，便于回放对齐）。"""
    return format(abs(hash(seed)) % (1 << 63), "016x")


def _attrs(pairs: list[tuple[str, Any]]) -> list[dict]:
    return [{"key": str(k), "value": {"stringValue": str(v)[:500]}} for k, v in pairs]


def span_tree_to_otlp(trace_tree: dict, run_id: str = "",
                      attributes: dict | None = None) -> dict:
    """把 KernelResult.meta['trace'] 的 span 树转换为 OTLP/JSON 导出对象。"""
    now_ns = int(time.time() * 1e9)
    duration_ms = int(trace_tree.get("duration_ms") or 0)
    trace_id = format(abs(hash((run_id, trace_tree.get("name", "")))) % (1 << 127),
                      "032x")
    root_id = _sid(("root", run_id))

    pairs = [("agent.run_id", run_id)]
    pairs += [(k, v) for k, v in (trace_tree.get("meta") or {}).items()]
    pairs += [(k, v) for k, v in (attributes or {}).items()]
    root_span: dict = {
        "traceId": trace_id,
        "spanId": root_id,
        "parentSpanId": "",
        "name": str(trace_tree.get("name") or "run"),
        "kind": 1,
        "startTimeUnixNano": str(now_ns - duration_ms * 1_000_000),
        "endTimeUnixNano": str(now_ns),
        "attributes": _attrs(pairs),
        "status": {"code": 1},
    }

    flat: list[dict] = [root_span]

    def walk(spans: list[dict], parent_id: str) -> None:
        for sp in spans:
            d = int(sp.get("duration_ms") or 0)
            entry: dict = {
                "traceId": trace_id,
                "spanId": _sid((sp.get("name"), parent_id, len(flat))),
                "parentSpanId": parent_id,
                "name": str(sp.get("name") or "span"),
                "kind": 1,
                "startTimeUnixNano": str(now_ns),
                "endTimeUnixNano": str(now_ns + d * 1_000_000),
                "attributes": _attrs(list((sp.get("meta") or {}).items())),
                "status": {"code": 1},
            }
            if sp.get("type"):
                entry["attributes"].append(
                    {"key": "span.type", "value": {"stringValue": str(sp["type"])}})
            flat.append(entry)
            walk(sp.get("children") or [], entry["spanId"])

    walk(trace_tree.get("children") or [], root_id)

    return {
        "resourceSpans": [{
            "resource": {"attributes": _attrs([
                ("service.name", "agent_core"),
                ("agent.run_id", run_id),
            ])},
            "scopeSpans": [{
                "scope": {"name": "agent_core.kernel", "version": "1.0.0"},
                "spans": flat,
            }],
        }],
    }
