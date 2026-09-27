"""执行 Trace 收集器：单次执行的 span tree 记录。

用于可观测性：每次 run_oneshot / stream 生成完整 trace，
包含 LLM 调用、工具调用、规划阶段、修正循环等 span。
产物写入 KernelResult.meta["trace"]，供 /agent/metrics 聚合。
"""
import time
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Span:
    """单个执行跨度。"""

    name: str
    span_type: str  # "llm_call" | "tool_call" | "plan_phase" | "correction" | "root"
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: int = 0
    meta: dict = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)
    _parent: "Span | None" = field(default=None, repr=False)

    def end(self, **meta) -> None:
        """结束 span，记录结束时间和耗时。"""
        self.end_time = time.monotonic()
        self.duration_ms = int((self.end_time - self.start_time) * 1000)
        self.meta.update(meta)

    def to_dict(self) -> dict:
        """序列化为 dict（递归子 span）。"""
        d = {
            "name": self.name,
            "type": self.span_type,
            "duration_ms": self.duration_ms,
        }
        if self.meta:
            d["meta"] = self.meta
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


class ExecutionTracer:
    """单次执行的 trace 收集器。

    用法：
        tracer = ExecutionTracer("run_oneshot")
        with tracer.span("llm_query", "llm_call") as s:
            # ... 执行 LLM 查询
            s.end(tokens=100, cost_usd=0.01)
        result.meta["trace"] = tracer.to_dict()
    """

    def __init__(self, trace_name: str = "execution"):
        self._root = Span(name=trace_name, span_type="root",
                          start_time=time.monotonic())
        self._current: Span = self._root
        self._span_count: int = 0

    def start_span(self, name: str, span_type: str) -> Span:
        """开始一个新 span，作为当前 span 的子 span。"""
        span = Span(name=name, span_type=span_type,
                    start_time=time.monotonic(), _parent=self._current)
        self._current.children.append(span)
        self._current = span
        self._span_count += 1
        return span

    def end_span(self, span: Span | None = None, **meta) -> None:
        """结束当前 span（或指定 span），回到父 span。"""
        target = span or self._current
        if target is self._root:
            return  # root 不能结束
        target.end(**meta)
        if target._parent is not None:
            self._current = target._parent

    def span(self, name: str, span_type: str) -> "_SpanContext":
        """上下文管理器形式的 span（推荐用法）。"""
        return _SpanContext(self, name, span_type)

    def finish(self) -> None:
        """结束整个 trace（结束 root span）。"""
        self._root.end(total_spans=self._span_count)

    def to_dict(self) -> dict:
        """输出完整 trace tree。"""
        if self._root.duration_ms == 0:
            self.finish()
        return self._root.to_dict()

    @property
    def span_count(self) -> int:
        return self._span_count


class _SpanContext:
    """span 的上下文管理器包装。"""

    def __init__(self, tracer: ExecutionTracer, name: str, span_type: str):
        self._tracer = tracer
        self._name = name
        self._type = span_type
        self._span: Span | None = None

    def __enter__(self) -> Span:
        self._span = self._tracer.start_span(self._name, self._type)
        return self._span

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._span is not None:
            meta = {}
            if exc_type is not None:
                meta["error"] = str(exc_val or exc_type.__name__)
            self._tracer.end_span(self._span, **meta)
