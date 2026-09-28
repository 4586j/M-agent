"""brain_api POST /recommend 端点测试：AI 推荐/解释顾问（独立上下文）。

覆盖：仅建议（不派发）、auto-apply（经安全关卡派发首要角色）、
should_dispatch=False 不派发、顾问未配置/未启用、拦截指令 auto-apply 不绕过安全关卡、
enable_subagents 透传到派发、/dispatch 透传 enable_subagents。
用假 AdvisorService 与假 Dispatcher，避免真实 LLM 调用。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from agent_core.advisor import AdvisorResult
from brain_api import create_brain_app
from config import Config
from dispatcher import Dispatcher, DispatchResult
from task_store import TaskStore
from worker_registry import WorkerRegistry


class _FakeAdvisor:
    def __init__(self, result, enabled=True, auto_apply=False):
        self._result = result
        self.config = SimpleNamespace(advisor_enabled=enabled,
                                      advisor_auto_apply=auto_apply)
        self.calls = []

    def recommend(self, task, mode="all", finished_output=None):
        self.calls.append({"task": task, "mode": mode,
                           "finished_output": finished_output})
        return self._result


class _CapDispatcher(Dispatcher):
    def __init__(self):
        self.calls = []

    def dispatch(self, role, prompt, context_memory=None, model=None,
                 use_experience=True, enable_subagents=None,
                 worker_id=None):
        self.calls.append({"role": role, "prompt": prompt,
                           "enable_subagents": enable_subagents})
        return DispatchResult("task-adv1", "done", output="ok",
                              artifacts=[], error=None)

    def redispatch(self, task_id, model=None, worker_id=None):
        raise KeyError(task_id)


def _client(tmp_path, advisor=None, dispatcher=None):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / "tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / "tasks"),
                 security_llm=False)
    app = create_brain_app(reg, store, cfg, dispatcher=dispatcher, advisor=advisor)
    return TestClient(app)


def _result(should=True, roles=None):
    return AdvisorResult(should_dispatch=should, roles=roles or ["executor"],
                         plan=[{"id": 1, "action": "do", "depends_on": []}],
                         explanation="因为可拆步", risks=["r1"], mode="all")


def test_recommend_advisory_only_by_default(tmp_path):
    advisor = _FakeAdvisor(_result(should=True))
    disp = _CapDispatcher()
    client = _client(tmp_path, advisor=advisor, dispatcher=disp)
    r = client.post("/recommend", json={"task": "分析数据并汇总", "mode": "all"})
    body = r.json()
    assert body["status"] == "ok"
    assert body["recommendation"]["should_dispatch"] is True
    assert body["recommendation"]["roles"] == ["executor"]
    assert body["recommendation"]["explanation"] == "因为可拆步"
    assert body["auto_applied"] is False
    assert body["dispatched"] == []
    assert disp.calls == []           # 仅建议，未派发
    assert advisor.calls[0]["mode"] == "all"


def test_recommend_auto_apply_dispatches_primary_role(tmp_path):
    advisor = _FakeAdvisor(_result(should=True, roles=["executor"]))
    disp = _CapDispatcher()
    client = _client(tmp_path, advisor=advisor, dispatcher=disp)
    r = client.post("/recommend", json={
        "task": "统计 workspace/data.csv 行数并报告", "auto_apply": True,
        "enable_subagents": False})
    body = r.json()
    assert body["auto_applied"] is True
    assert body["dispatched"][0]["role"] == "executor"
    assert body["dispatched"][0]["task_id"] == "task-adv1"
    assert len(disp.calls) == 1
    # enable_subagents 每次请求覆盖透传到派发
    assert disp.calls[0]["enable_subagents"] is False


def test_recommend_auto_apply_skips_when_should_not_dispatch(tmp_path):
    advisor = _FakeAdvisor(_result(should=False))
    disp = _CapDispatcher()
    client = _client(tmp_path, advisor=advisor, dispatcher=disp)
    body = client.post("/recommend", json={"task": "1+1=?", "auto_apply": True}).json()
    assert body["auto_applied"] is False
    assert disp.calls == []


def test_recommend_global_auto_apply_default(tmp_path):
    # 全局 advisor_auto_apply=True 且请求未覆盖 → 自动派发
    advisor = _FakeAdvisor(_result(should=True), auto_apply=True)
    disp = _CapDispatcher()
    client = _client(tmp_path, advisor=advisor, dispatcher=disp)
    body = client.post("/recommend", json={"task": "拆分并执行复杂任务"}).json()
    assert body["auto_applied"] is True
    assert len(disp.calls) == 1


def test_recommend_auto_apply_blocked_by_security(tmp_path):
    # 危险指令：auto-apply 也绝不绕过安全关卡 → blocked，不派发
    advisor = _FakeAdvisor(_result(should=True))
    disp = _CapDispatcher()
    client = _client(tmp_path, advisor=advisor, dispatcher=disp)
    body = client.post("/recommend", json={
        "task": "清理磁盘：rm -rf / 然后报告", "auto_apply": True}).json()
    assert body["auto_applied"] is False
    assert body["dispatched"][0]["status"] == "blocked"
    assert disp.calls == []


def test_recommend_advisor_disabled(tmp_path):
    advisor = _FakeAdvisor(_result(), enabled=False)
    client = _client(tmp_path, advisor=advisor, dispatcher=_CapDispatcher())
    body = client.post("/recommend", json={"task": "x"}).json()
    assert body["status"] == "failed"
    assert "advisor" in body["error"]


def test_recommend_advisor_not_configured(tmp_path):
    client = _client(tmp_path, advisor=None, dispatcher=_CapDispatcher())
    body = client.post("/recommend", json={"task": "x"}).json()
    assert body["status"] == "failed"


def test_recommend_no_dispatcher_auto_apply_reports_error(tmp_path):
    advisor = _FakeAdvisor(_result(should=True), auto_apply=True)
    client = _client(tmp_path, advisor=advisor, dispatcher=None)
    body = client.post("/recommend", json={"task": "复杂任务"}).json()
    assert body["auto_applied"] is False
    assert "dispatcher" in body["error"]


def test_dispatch_passes_enable_subagents(tmp_path):
    disp = _CapDispatcher()
    client = _client(tmp_path, dispatcher=disp)
    client.post("/dispatch", json={
        "prompt": "普通任务", "role": "executor", "force": True,
        "enable_subagents": False})
    assert disp.calls[0]["enable_subagents"] is False


def test_dispatch_enable_subagents_defaults_none(tmp_path):
    disp = _CapDispatcher()
    client = _client(tmp_path, dispatcher=disp)
    client.post("/dispatch", json={"prompt": "普通任务", "role": "executor",
                                   "force": True})
    assert disp.calls[0]["enable_subagents"] is None
