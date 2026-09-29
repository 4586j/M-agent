"""OTLP/JSON 导出测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from otlp_export import span_tree_to_otlp


def _tree():
    return {
        "name": "run_oneshot", "type": "root", "duration_ms": 5000,
        "meta": {"total_spans": 2},
        "children": [
            {"name": "correction", "type": "correction",
             "duration_ms": 1200, "meta": {"attempts": 1}},
        ],
    }


def test_otlp_shape():
    out = span_tree_to_otlp(_tree(), run_id="run-1",
                            attributes={"persona": "executor"})
    rs = out["resourceSpans"]
    assert rs[0]["resource"]["attributes"][0]["key"] == "service.name"
    scope = rs[0]["scopeSpans"][0]
    assert scope["scope"]["name"] == "agent_core.kernel"
    spans = scope["spans"]
    assert len(spans) == 2  # 根 + correction
    root = spans[0]
    assert root["parentSpanId"] == "" and root["name"] == "run_oneshot"
    child = spans[1]
    assert child["parentSpanId"] == root["spanId"] and child["name"] == "correction"
    # 纳秒时长
    assert int(child["endTimeUnixNano"]) - int(child["startTimeUnixNano"]) == 1_200_000_000


def test_otlp_attributes_propagated():
    out = span_tree_to_otlp(_tree(), run_id="run-2", attributes={"model": "qwen"})
    root = out["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    keys = [a["key"] for a in root["attributes"]]
    assert "agent.run_id" in keys and "model" in keys


def test_otlp_empty_tree():
    out = span_tree_to_otlp({}, run_id="run-3")
    spans = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1  # 仅根 span
