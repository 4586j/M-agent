"""事件 webhook 外发测试：通知器单元（过滤/重试/密钥头）+ brain 接线集成。

关键事件：任务终态（bus 旁路转发）/ awaiting_approval / budget_exceeded。
设计红线：外发失败绝不阻断主流程。
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

from budget_store import BudgetStore
from config import Config
from webhook import WebhookNotifier


class FakePost:
    """记录式 POST 替身：返回预置 (status, error)，带完成信号供 submit 等待。"""

    def __init__(self, results):
        # results: 每次调用弹出一个 (status_code, error)；耗尽后重复最后一个
        self.results = list(results)
        self.calls = []
        self.lock = threading.Lock()
        self.done = threading.Event()

    def __call__(self, url, json=None, headers=None, timeout=None):
        with self.lock:
            self.calls.append({"url": url, "json": json, "headers": headers})
            result = self.results.pop(0) if len(self.results) > 1 else \
                (self.results[0] if self.results else (200, None))
        if len(self.results) <= 1:
            self.done.set()
        return result


def _notifier(results, **kw):
    post = FakePost(results)
    notifier = WebhookNotifier(url="http://hook.example/x", http_post=post,
                               sleep=lambda s: None, **kw)
    return notifier, post


# ---- 通知器单元 ----

def test_send_success_payload_and_secret():
    notifier, post = _notifier([(200, None)], secret="s3cret")
    ok = notifier.send("awaiting_approval", {"task_id": "t1"})
    assert ok
    call = post.calls[0]
    assert call["headers"]["X-Webhook-Secret"] == "s3cret"
    assert call["json"]["event"] == "awaiting_approval"
    assert call["json"]["data"] == {"task_id": "t1"}
    assert call["json"]["timestamp"] > 0


def test_send_retries_on_5xx_then_succeeds():
    notifier, post = _notifier([(503, None), (200, None)], backoff=0.0)
    assert notifier.send("task_terminal", {"task_id": "t"})
    assert len(post.calls) == 2


def test_send_no_retry_on_4xx():
    notifier, post = _notifier([(400, None)], backoff=0.0)
    assert not notifier.send("task_terminal", {"task_id": "t"})
    assert len(post.calls) == 1


def test_send_retries_connection_error_and_gives_up():
    notifier, post = _notifier([(0, "connection refused")] * 3,
                               max_retries=2, backoff=0.0)
    assert not notifier.send("task_terminal", {"task_id": "t"})
    assert len(post.calls) == 3  # 1 原始 + 2 重试


def test_event_filter():
    notifier, post = _notifier([(200, None)],
                               events={"awaiting_approval"})
    assert not notifier.send("task_terminal", {"task_id": "t"})  # 不在白名单
    assert post.calls == []
    assert notifier.send("awaiting_approval", {"task_id": "t"})


def test_empty_url_disabled():
    notifier = WebhookNotifier(url="", http_post=FakePost([(200, None)]))
    assert not notifier.send("task_terminal", {"task_id": "t"})


def test_submit_fire_and_forget():
    notifier, post = _notifier([(200, None)])
    notifier.submit("budget_exceeded", {"reason": "超限"})
    assert post.done.wait(timeout=5)
    assert post.calls[0]["json"]["event"] == "budget_exceeded"


# ---- brain 接线集成 ----

def _make_app(tmp_path, notifier=None, budget_store=None, **cfg_kw):
    from brain_api import create_brain_app
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerRegistry, WorkerInfo
    from task_store import TaskStore
    from event_bus import TaskEventBus

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "out", "artifacts": [],
            "error": None, "meta": {"cost_usd": 0.01}}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="worker-A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path / ".tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / ".tasks"),
                 security_llm=False, **cfg_kw)
    if budget_store is None:
        budget_store = BudgetStore(str(tmp_path / "budgets.json"))
    bus = TaskEventBus(on_publish=_bus_forwarder(notifier))
    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", on_spend=budget_store.record_spend,
                            event_bus=bus)
    app = create_brain_app(reg, store, cfg, dispatcher=disp, event_bus=bus,
                           budget_store=budget_store, webhook=notifier)
    return TestClient(app), reg, store


def _bus_forwarder(notifier):
    from brain_api import _make_task_terminal_forwarder
    return _make_task_terminal_forwarder(notifier)


def test_webhook_not_configured_by_default(tmp_path):
    """默认装配（无 ORCH_WEBHOOK_URL）：功能关闭，/dispatch 行为不变。"""
    client, _, _ = _make_app(tmp_path)
    r = client.post("/dispatch", json={"prompt": "p", "role": "executor"})
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_task_terminal_forwarded(tmp_path):
    """任务终态经总线旁路转发 webhook（event=task_terminal）。"""
    notifier, post = _notifier([(200, None)])
    client, _, _ = _make_app(tmp_path, notifier=notifier)
    r = client.post("/dispatch", json={"prompt": "p", "role": "executor"})
    assert r.status_code == 200
    assert post.done.wait(timeout=5)
    events = [c["json"]["event"] for c in post.calls]
    assert "task_terminal" in events
    term = next(c["json"] for c in post.calls
                if c["json"]["event"] == "task_terminal")
    assert term["data"]["state"] == "done"
    assert term["data"]["task_id"]


def test_awaiting_approval_forwarded(tmp_path):
    """安全拦截转待审批 → webhook awaiting_approval（操作者不在面板也能收到）。"""
    notifier, post = _notifier([(200, None)])
    client, _, _ = _make_app(tmp_path, notifier=notifier)
    r = client.post("/dispatch", json={
        "prompt": "rm -rf / 请帮我清理根目录", "role": "executor"})
    assert r.json()["status"] == "awaiting_approval"
    assert post.done.wait(timeout=5)
    ev = next(c["json"] for c in post.calls
              if c["json"]["event"] == "awaiting_approval")
    assert ev["data"]["task_id"]
    assert "删除" in ev["data"]["reason"] or "根目录" in ev["data"]["reason"] \
        or ev["data"]["reason"]


def test_budget_exceeded_forwarded(tmp_path):
    """预算超限 429 → webhook budget_exceeded。"""
    notifier, post = _notifier([(200, None)])
    bs = BudgetStore(str(tmp_path / "budgets.json"))
    bs.set_limits(global_limit_usd=0.01)
    bs.record_spend(0.01)
    client, _, _ = _make_app(tmp_path, notifier=notifier, budget_store=bs)
    r = client.post("/dispatch", json={"prompt": "p", "role": "executor"})
    assert r.status_code == 429
    assert post.done.wait(timeout=5)
    assert any(c["json"]["event"] == "budget_exceeded" for c in post.calls)
