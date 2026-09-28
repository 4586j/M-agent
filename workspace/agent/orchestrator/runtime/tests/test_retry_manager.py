import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retry_manager import RetryManager
from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry, WorkerInfo
from dispatcher import DispatchResult


def make_info(wid="worker-A"):
    return WorkerInfo(
        worker_id=wid, role="executor", host="127.0.0.1", port=8000,
        capabilities={}, last_heartbeat=0.0,
    )


def make_record(task_id="task-1", status="pending", worker_id="worker-A",
                retry_count=0, max_retries=0, last_retry_at="", meta=None):
    return TaskRecord(
        task_id=task_id, role="executor", prompt="do x",
        worker_id=worker_id, status=status,
        created_at="100", updated_at="100",
        output=None, artifacts=[], error=None,
        retry_count=retry_count, max_retries=max_retries,
        last_retry_at=last_retry_at, meta=meta,
    )


class FakeDispatcher:
    def __init__(self, result_status="done"):
        self.calls = []
        self.result_status = result_status

    def dispatch(self, role, prompt, context_memory=None, model=None,
                 worker_id=None, provider=None):
        self.calls.append({"role": role, "prompt": prompt, "worker_id": worker_id,
                           "model": model, "provider": provider})
        return DispatchResult(task_id="task-new", status=self.result_status)

    def redispatch(self, task_id, model=None, worker_id=None, provider=None):
        return self.dispatch(role="executor", prompt="redispatched",
                             worker_id=worker_id)


def test_sweep_leaves_awaiting_approval_untouched(tmp_path):
    """阶段四审批挂起态：等待人工裁决的任务不被标 lost、不参与自动重试。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-h", status="awaiting_approval", worker_id="worker-A"))
    reg._now = lambda: 200.0  # worker 心跳超时（若为 running 会被标 lost）
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    result = mgr.sweep_once()
    assert result["marked_lost"] == 0
    assert result["retried"] == 0
    got = store.get("task-h")
    assert got.status == "awaiting_approval"  # 挂起态原样保留
    assert fake_disp.calls == []               # 无任何重试派发


def test_sweep_leaves_dispatching_untouched(tmp_path):
    """approve 派发占位态（dispatching）：worker 心跳超时也不被 sweeper 标 lost。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-d", status="dispatching", worker_id="worker-A"))
    reg._now = lambda: 200.0
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    result = mgr.sweep_once()
    assert result["marked_lost"] == 0
    assert result["retried"] == 0
    assert store.get("task-d").status == "dispatching"  # 占位态原样保留
    assert fake_disp.calls == []

    # dispatching 不参与自动重试
    assert [r.task_id for r in store.list_retryable()] == []


def test_sweep_marks_dead_worker_tasks_as_lost(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=10, now_fn=lambda: 100.0)
    reg.register(make_info())
    store.write(make_record("task-1", status="running"))

    # Worker dies (time advances past timeout)
    reg._now = lambda: 200.0
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    result = mgr.sweep_once()
    assert result["marked_lost"] == 1
    assert store.get("task-1").status == "lost"


def test_sweep_retries_eligible_lost_tasks(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-1", status="lost", retry_count=0, max_retries=3))

    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 100.0, ts_fn=lambda: "100")

    result = mgr.sweep_once()
    assert result["retried"] == 1
    assert len(fake_disp.calls) == 1
    assert fake_disp.calls[0]["role"] == "executor"


def test_sweep_retries_eligible_failed_tasks(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-1", status="failed", retry_count=1, max_retries=3))

    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 100.0, ts_fn=lambda: "100")

    result = mgr.sweep_once()
    assert result["retried"] == 1
    rec = store.get("task-1")
    assert rec.retry_count == 2
    assert rec.last_retry_at == "100"


def test_sweep_skips_tasks_at_max_retries(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-1", status="lost", retry_count=3, max_retries=3))

    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 100.0, ts_fn=lambda: "100")

    result = mgr.sweep_once()
    assert result["retried"] == 0
    assert len(fake_disp.calls) == 0


def test_sweep_respects_backoff(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    # retry_count=1, base_delay=10 → need 10 * 2^1 = 20s elapsed
    store.write(make_record("task-1", status="lost", retry_count=1, max_retries=3,
                            last_retry_at="90"))

    fake_disp = FakeDispatcher()
    # Only 5s elapsed (now=95), need 20s
    mgr = RetryManager(store, reg, fake_disp, retry_base_delay=10.0,
                       now_fn=lambda: 95.0, ts_fn=lambda: "95")

    result = mgr.sweep_once()
    assert result["retried"] == 0

    # Now advance time past backoff
    mgr._now = lambda: 115.0
    result = mgr.sweep_once()
    assert result["retried"] == 1


def test_sweep_skips_zero_max_retries(tmp_path):
    """Tasks with max_retries=0 (default) should never be retried."""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-1", status="lost", retry_count=0, max_retries=0))

    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 100.0, ts_fn=lambda: "100")

    result = mgr.sweep_once()
    assert result["retried"] == 0


def test_check_worker_healing_redispatches_lost_tasks(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info("worker-A"))
    store.write(make_record("task-1", status="lost", worker_id="worker-A"))
    store.write(make_record("task-2", status="done", worker_id="worker-A"))

    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 100.0, ts_fn=lambda: "100")

    healed = mgr.check_worker_healing("worker-A")
    assert healed == 1
    assert len(fake_disp.calls) == 1
    # role 一致时新派发定点回刚恢复的节点（与 old 记录 worker_id 改写一致）
    assert fake_disp.calls[0]["worker_id"] == "worker-A"
    rec = store.get("task-1")
    assert rec.status == "running"
    assert rec.retry_count == 1


def test_check_worker_healing_unknown_worker(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp)

    healed = mgr.check_worker_healing("unknown-worker")
    assert healed == 0


def test_sweep_no_dead_workers_no_retryable(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record("task-1", status="done"))

    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 10.0, ts_fn=lambda: "10")

    result = mgr.sweep_once()
    assert result["marked_lost"] == 0
    assert result["retried"] == 0


def test_sweep_multiple_dead_workers_and_tasks(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=10, now_fn=lambda: 100.0)
    reg.register(make_info("worker-A"))
    reg.register(make_info("worker-B"))
    store.write(make_record("task-1", status="running", worker_id="worker-A"))
    store.write(make_record("task-2", status="running", worker_id="worker-B"))
    store.write(make_record("task-3", status="running", worker_id="worker-B"))

    # Both workers die
    reg._now = lambda: 200.0
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    result = mgr.sweep_once()
    assert result["marked_lost"] == 3
    assert store.get("task-1").status == "lost"
    assert store.get("task-2").status == "lost"
    assert store.get("task-3").status == "lost"


def test_start_and_stop(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, sweeper_interval=1)

    mgr.start()
    assert mgr._thread is not None
    assert mgr._thread.daemon is True

    mgr.stop()
    mgr._thread.join(timeout=3)
    assert not mgr._thread.is_alive()


def test_retry_inherits_model_and_provider(tmp_path):
    """重试继承原任务的 model 与 provider（同名经 resolver 重新解析）。

    此前重试派发丢 model 覆盖（历史缺陷）；provider 同名重试且密钥不入 meta。
    """
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record(
        "task-r", status="failed", max_retries=1,
        meta={"model": "m1", "provider": "gw"}))
    fake_disp = FakeDispatcher()
    resolved = {"name": "gw", "model": "sonnet", "base_url": "https://gw",
                "auth_token": "tok"}
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 200.0,
                       ts_fn=lambda: "200",
                       provider_resolver=lambda name: resolved)
    mgr._retry_eligible()
    call = fake_disp.calls[-1]
    assert call["model"] == "m1"                  # model 覆盖被继承
    assert call["provider"]["name"] == "gw"       # provider 同名重试
    assert call["provider"]["auth_token"] == "tok"


def test_retry_unresolvable_provider_falls_back(tmp_path):
    """provider 名在重试时已不可解析 → 告警并按无 provider 重试（不阻断）。"""
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())
    store.write(make_record(
        "task-r2", status="failed", max_retries=1,
        meta={"provider": "gone"}))
    fake_disp = FakeDispatcher()
    mgr = RetryManager(store, reg, fake_disp, now_fn=lambda: 200.0,
                       ts_fn=lambda: "200",
                       provider_resolver=lambda name: None)
    mgr._retry_eligible()
    assert fake_disp.calls[-1]["provider"] is None
