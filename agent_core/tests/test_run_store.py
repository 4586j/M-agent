"""RunStore（SQLite 持久化执行日志）测试。

覆盖 Slice 3 验收门：重启对账、准入饱和、取消竞态（CAS）、崩溃后副作用不盲目
重放、审批过期/绑定、JSON 幂等导入、按 workspace/session 串行化。全程用可注入
墙钟，不触网、不依赖真实执行。
"""
import json
from pathlib import Path

import pytest

from agent_core.run_store import (SCHEMA_VERSION, ClaimResult, RunStore,
                                  default_journal_path, open_journal)


class _Clock:
    """可手动推进的墙钟，供截止/过期/对账测试精确控制时间。"""

    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = RunStore(tmp_path / "runtime.sqlite3", max_active=1, now_fn=clock)
    yield s
    s.close()


# ---- schema / 连接 ----

def test_schema_version_persisted(tmp_path):
    db = tmp_path / "runtime.sqlite3"
    with RunStore(db) as s:
        assert s.schema_version == SCHEMA_VERSION
    # 重新打开：版本持久化，WAL 落盘
    with RunStore(db) as s2:
        assert s2.schema_version == SCHEMA_VERSION


def test_wal_journal_mode_enabled(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_rejects_newer_schema(tmp_path, monkeypatch):
    db = tmp_path / "runtime.sqlite3"
    with RunStore(db) as s:
        s._conn.execute("UPDATE schema_meta SET value='999' "
                        "WHERE key='schema_version'")
    # 代码版本低于库版本 → 拒绝打开（避免误写不兼容结构）
    with pytest.raises(RuntimeError, match="too new"):
        RunStore(db)


# ---- 任务 CRUD / CAS ----

def test_create_and_get_task(store):
    t = store.create_task("t1", "executor", "做点事", workspace_key="w1",
                          meta={"model": "sonnet"}, trace_id="trc-1")
    assert t["task_id"] == "t1"
    assert t["status"] == "pending"
    assert t["meta"] == {"model": "sonnet"}
    assert t["verdict"] is None
    assert store.get_task("t1")["prompt"] == "做点事"
    assert store.get_task("nope") is None


def test_create_task_rejects_bad_status(store):
    with pytest.raises(ValueError):
        store.create_task("t1", "r", "p", status="bogus")


def test_cas_status_only_from_expected(store):
    store.create_task("t1", "r", "p")
    # pending → running 命中
    assert store.cas_task_status("t1", ("pending",), "running") is True
    # 再用错误的 expected → 不命中
    assert store.cas_task_status("t1", ("pending",), "done") is False
    assert store.get_task("t1")["status"] == "running"


def test_cas_writes_fields_atomically(store):
    store.create_task("t1", "r", "p")
    ok = store.cas_task_status("t1", ("pending",), "done", output="结果",
                               verdict={"flag": False}, meta={"turns": 3},
                               deadline_at=1234.5)
    assert ok is True
    t = store.get_task("t1")
    assert t["status"] == "done"
    assert t["output"] == "结果"
    assert t["verdict"] == {"flag": False}
    assert t["meta"] == {"turns": 3}
    assert t["deadline_at"] == 1234.5


def test_cancel_cannot_be_overwritten(store):
    """取消竞态门：任务已 cancelled（终态）后，迟到的收尾写入不得覆盖。"""
    store.create_task("t1", "r", "p")
    store.cas_task_status("t1", ("pending",), "running")
    # 取消：running → cancelled
    assert store.cas_task_status("t1", ("running",), "cancelled") is True
    # 派发线程迟到收尾，expected 只含非终态 → 必须失败，cancelled 保持
    assert store.cas_task_status("t1", ("pending", "running"), "done",
                                 output="迟到的成功") is False
    t = store.get_task("t1")
    assert t["status"] == "cancelled"
    assert t["output"] is None


def test_cas_empty_expected_raises(store):
    store.create_task("t1", "r", "p")
    with pytest.raises(ValueError):
        store.cas_task_status("t1", (), "done")


def test_list_tasks_by_status(store):
    store.create_task("t1", "r", "p")
    store.create_task("t2", "r", "p", status="failed")
    assert [t["task_id"] for t in store.list_tasks("failed")] == ["t2"]
    assert len(store.list_tasks()) == 2


# ---- 准入 / 认领 ----

def test_try_claim_admits_and_sets_running(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    res = store.try_claim("t1", worker="local")
    assert isinstance(res, ClaimResult)
    assert res.admitted is True and res.attempt_id
    assert store.get_task("t1")["status"] == "running"
    assert store.count_active() == 1
    att = store.get_attempt(res.attempt_id)
    assert att["status"] == "running" and att["worker"] == "local"
    assert att["seq"] == 1


def test_admission_saturation_rejects_excess(store):
    """准入饱和门：max_active=1 时第二个并发认领被显式拒绝。"""
    store.create_task("t1", "r", "p", workspace_key="w1")
    store.create_task("t2", "r", "p", workspace_key="w2")
    assert store.try_claim("t1").admitted is True
    res2 = store.try_claim("t2")
    assert res2.admitted is False
    assert res2.reason == "saturated"
    assert store.count_active() == 1


def test_admission_bound_configurable(tmp_path, clock):
    s = RunStore(tmp_path / "r.sqlite3", max_active=2, now_fn=clock)
    try:
        for i in range(3):
            s.create_task(f"t{i}", "r", "p", workspace_key=f"w{i}")
        assert s.try_claim("t0").admitted is True
        assert s.try_claim("t1").admitted is True
        res = s.try_claim("t2")
        assert res.admitted is False and res.reason == "saturated"
        assert s.count_active() == 2
    finally:
        s.close()


def test_per_workspace_serialization(tmp_path, clock):
    """同 workspace 串行化：即便全局额度未满，同键在飞也拒绝。"""
    s = RunStore(tmp_path / "r.sqlite3", max_active=5, now_fn=clock)
    try:
        s.create_task("t1", "r", "p", workspace_key="shared")
        s.create_task("t2", "r", "p", workspace_key="shared")
        assert s.try_claim("t1").admitted is True
        res = s.try_claim("t2")
        assert res.admitted is False and res.reason == "busy:workspace"
    finally:
        s.close()


def test_per_session_serialization(tmp_path, clock):
    s = RunStore(tmp_path / "r.sqlite3", max_active=5, now_fn=clock)
    try:
        s.create_task("t1", "r", "p", workspace_key="w1", session_key="sess")
        s.create_task("t2", "r", "p", workspace_key="w2", session_key="sess")
        assert s.try_claim("t1").admitted is True
        res = s.try_claim("t2")
        assert res.admitted is False and res.reason == "busy:session"
    finally:
        s.close()


def test_claim_unknown_task_rejected(store):
    res = store.try_claim("ghost")
    assert res.admitted is False and res.reason == "no_task"


def test_claim_terminal_task_rejected(store):
    store.create_task("t1", "r", "p")
    store.cas_task_status("t1", ("pending",), "done")
    res = store.try_claim("t1")
    assert res.admitted is False and res.reason == "terminal:done"


def test_finish_attempt_is_cas_and_frees_admission(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    aid = store.try_claim("t1").attempt_id
    assert store.finish_attempt(aid, "done", result={"output": "ok"}) is True
    att = store.get_attempt(aid)
    assert att["status"] == "done" and att["result"] == {"output": "ok"}
    assert att["finished_at"] is not None
    # 终态后不再占用额度
    assert store.count_active() == 0
    # 重复 finish（已终态）→ CAS 不命中
    assert store.finish_attempt(aid, "failed") is False


def test_attempt_seq_increments(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    a1 = store.try_claim("t1").attempt_id
    store.finish_attempt(a1, "failed")
    # 释放额度后再认领，seq 递增（重试=新尝试，不复活旧的）
    a2 = store.try_claim("t1").attempt_id
    assert store.get_attempt(a1)["seq"] == 1
    assert store.get_attempt(a2)["seq"] == 2
    assert len(store.list_attempts("t1")) == 2


def test_set_attempt_host(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    aid = store.try_claim("t1").attempt_id
    assert store.set_attempt_host(aid, 4242) is True
    assert store.get_attempt(aid)["host_pid"] == 4242
    # finish 清空 host_pid（进程已收）
    store.finish_attempt(aid, "done")
    assert store.get_attempt(aid)["host_pid"] is None


# ---- 重启对账 ----

def test_reconcile_marks_inflight_interrupted(store):
    """重启门：崩溃时在飞的尝试/任务被对账为 interrupted，不假设仍活着。"""
    store.create_task("t1", "r", "p", workspace_key="w1")
    store.create_task("t2", "r", "p", workspace_key="w2")
    store.try_claim("t1")
    # t2 未认领（pending），不应被对账影响
    affected = store.reconcile_interrupted()
    assert affected == ["t1"]
    assert store.get_task("t1")["status"] == "interrupted"
    assert store.get_task("t2")["status"] == "pending"
    assert store.count_active() == 0
    # 幂等：再次对账无新增
    assert store.reconcile_interrupted() == []


def test_reconcile_does_not_touch_terminal(store):
    store.create_task("t1", "r", "p", workspace_key="w1")
    aid = store.try_claim("t1").attempt_id
    store.finish_attempt(aid, "done")
    store.cas_task_status("t1", ("running",), "done", output="x")
    assert store.reconcile_interrupted() == []
    assert store.get_task("t1")["status"] == "done"


def test_reconcile_across_reopen(tmp_path, clock):
    """真实重启：新开一个 RunStore 指向同一 db，对账上个进程遗留的在飞尝试。"""
    db = tmp_path / "runtime.sqlite3"
    s1 = RunStore(db, max_active=1, now_fn=clock)
    s1.create_task("t1", "r", "p", workspace_key="w1")
    s1.try_claim("t1")
    s1.close()  # 模拟进程退出（未 finish）

    s2 = RunStore(db, max_active=1, now_fn=clock)
    try:
        assert s2.count_active() == 1  # 库里仍记为在飞
        assert s2.reconcile_interrupted() == ["t1"]
        assert s2.get_task("t1")["status"] == "interrupted"
        # 对账后额度释放，可重新认领
        assert s2.try_claim("t1").admitted is True
    finally:
        s2.close()


# ---- 事件账本 ----

def test_append_and_list_events(store):
    store.create_task("t1", "r", "p")
    store.append_event("t1", "status", {"state": "running"}, attempt_id="a1")
    store.append_event("t1", "tool_use", {"name": "Read"})
    evs = store.list_events("t1")
    assert [e["type"] for e in evs] == ["status", "tool_use"]
    assert evs[0]["payload"] == {"state": "running"}
    assert evs[0]["attempt_id"] == "a1"
    assert evs[0]["id"] < evs[1]["id"]  # 单调递增
    assert store.list_events("t1", limit=1) == evs[:1]


# ---- 审批：绑定 + 过期 ----

def test_approval_binding_matches_only_same_action(store):
    """审批绑定到 action_id/args_hash/resource：不同动作不能复用同一裁决。"""
    store.record_approval("Bash", args_hash="h1", resource="rm -rf /tmp/x",
                          task_id="t1", decision="approved", ttl=60)
    assert store.get_valid_approval("Bash", args_hash="h1",
                                    resource="rm -rf /tmp/x") is not None
    # 参数变了（args_hash 不同）→ 无有效审批
    assert store.get_valid_approval("Bash", args_hash="h2",
                                    resource="rm -rf /tmp/x") is None
    # 资源变了 → 无有效审批
    assert store.get_valid_approval("Bash", args_hash="h1",
                                    resource="rm -rf /") is None
    # 动作变了 → 无有效审批
    assert store.get_valid_approval("Write", args_hash="h1",
                                    resource="rm -rf /tmp/x") is None


def test_stale_approval_rejected_after_expiry(store, clock):
    """过期审批门：超过 ttl 后同一条审批不再有效。"""
    store.record_approval("Bash", args_hash="h1", resource="r",
                          decision="approved", ttl=30)
    assert store.get_valid_approval("Bash", args_hash="h1", resource="r")
    clock.advance(31)
    assert store.get_valid_approval("Bash", args_hash="h1", resource="r") is None


def test_approval_no_ttl_never_expires(store, clock):
    store.record_approval("Read", args_hash="h", resource="f",
                          decision="approved")
    clock.advance(10_000)
    assert store.get_valid_approval("Read", args_hash="h", resource="f")


def test_approval_attempt_scoped(store):
    store.record_approval("Bash", args_hash="h", resource="r", attempt_id="a1",
                          decision="approved")
    assert store.get_valid_approval("Bash", args_hash="h", resource="r",
                                    attempt_id="a1")
    # 新尝试（a2）不能沿用旧尝试的审批
    assert store.get_valid_approval("Bash", args_hash="h", resource="r",
                                    attempt_id="a2") is None


def test_pending_and_denied_not_valid(store):
    store.record_approval("Bash", args_hash="h", resource="r", decision="pending")
    assert store.get_valid_approval("Bash", args_hash="h", resource="r") is None
    store.record_approval("Bash", args_hash="h2", resource="r", decision="denied")
    assert store.get_valid_approval("Bash", args_hash="h2", resource="r") is None


def test_record_approval_rejects_bad_decision(store):
    with pytest.raises(ValueError):
        store.record_approval("Bash", decision="maybe")


def test_latest_approval_returns_most_recent_regardless_of_validity(store, clock):
    """latest_approval 不判断过期/decision，返回最近一条匹配审批（供精确原因）。"""
    store.record_approval("Bash", args_hash="h", resource="r",
                          decision="approved", ttl=10)   # 即将过期
    clock.advance(20)
    # get_valid_approval 已因过期返回 None，但 latest_approval 仍能查到该条。
    assert store.get_valid_approval("Bash", args_hash="h", resource="r") is None
    row = store.latest_approval("Bash", args_hash="h", resource="r")
    assert row is not None and row["decision"] == "approved"


def test_latest_approval_filters_by_dimensions(store):
    store.record_approval("Bash", args_hash="h1", resource="r1",
                          attempt_id="a1", decision="denied")
    # 不传 args_hash/resource → 仅按 action_id + attempt_id 过滤
    assert store.latest_approval("Bash", attempt_id="a1")["args_hash"] == "h1"
    # 参数维度不匹配 → None
    assert store.latest_approval("Bash", args_hash="h2", attempt_id="a1") is None
    # 完全不存在的动作 → None
    assert store.latest_approval("Write") is None


# ---- 工具副作用账本 ----

def test_effect_lifecycle_committed(store):
    store.create_task("t1", "r", "p")
    eff = store.record_effect_start("Bash", "external", args_hash="h",
                                    task_id="t1", attempt_id="a1")
    assert eff["state"] == "started"
    # 已提交 → 不在未决列表
    store.set_effect_state(eff["effect_id"], "committed", outcome={"rc": 0})
    assert store.list_unresolved_effects(task_id="t1") == []
    assert store.get_effect(eff["effect_id"])["outcome"] == {"rc": 0}


def test_crash_after_effect_leaves_unresolved_not_replayed(store):
    """崩溃后副作用门：started 但未 committed → 未决；external 不可安全重放。"""
    store.create_task("t1", "r", "p")
    ext = store.record_effect_start("Bash", "external", task_id="t1")
    # 模拟崩溃：不 set committed，直接对账
    store.reconcile_interrupted()
    unresolved = store.list_unresolved_effects(task_id="t1")
    assert [e["effect_id"] for e in unresolved] == [ext["effect_id"]]
    assert unresolved[0]["state"] == "started"
    # external 结果未知 → 不可盲目重放
    assert RunStore.is_replay_safe(unresolved[0]) is False


def test_read_and_idempotent_effects_are_replay_safe(store):
    r = store.record_effect_start("Read", "read", task_id="t1")
    i = store.record_effect_start("CachePut", "idempotent", task_id="t1")
    assert RunStore.is_replay_safe(r) is True
    assert RunStore.is_replay_safe(i) is True


def test_effect_rejects_bad_kind_and_state(store):
    with pytest.raises(ValueError):
        store.record_effect_start("X", "bogus")
    eff = store.record_effect_start("Read", "read")
    with pytest.raises(ValueError):
        store.set_effect_state(eff["effect_id"], "bogus")


def test_list_unresolved_includes_unknown(store):
    e1 = store.record_effect_start("Bash", "external", task_id="t1")
    store.set_effect_state(e1["effect_id"], "unknown")
    e2 = store.record_effect_start("Read", "read", task_id="t1")
    store.set_effect_state(e2["effect_id"], "committed")
    unresolved = store.list_unresolved_effects()
    assert [e["effect_id"] for e in unresolved] == [e1["effect_id"]]


# ---- 幂等 JSON 导入 ----

def _json_record(task_id, status="done", output="x", **extra):
    base = {
        "task_id": task_id, "role": "executor", "prompt": "p",
        "worker_id": "local", "status": status,
        "created_at": "1700000000", "updated_at": "1700000001",
        "output": output, "artifacts": [], "error": None, "verdict": None,
        "retry_count": 0, "max_retries": 0, "last_retry_at": "",
        "trace_id": "trc-x", "meta": {"model": "sonnet"},
    }
    base.update(extra)
    return base


def test_import_json_records_idempotent(store):
    recs = [_json_record("t1"), _json_record("t2", status="failed", output=None)]
    r1 = store.import_json_records(recs)
    assert (r1.total, r1.imported, r1.skipped) == (2, 2, 0)
    # 再导入同一批：结果一致、无重复
    r2 = store.import_json_records(recs)
    assert r2.imported == 2
    assert len(store.list_tasks()) == 2
    t1 = store.get_task("t1")
    assert t1["status"] == "done" and t1["meta"] == {"model": "sonnet"}


def test_import_does_not_downgrade_terminal(store):
    """幂等 + 单调：已 done 的历史任务不被后来的非终态记录回退。"""
    store.import_json_records([_json_record("t1", status="done", output="good")])
    store.import_json_records([_json_record("t1", status="running", output=None)])
    t1 = store.get_task("t1")
    assert t1["status"] == "done"
    assert t1["output"] == "good"


def test_import_skips_missing_id(store):
    r = store.import_json_records([{"role": "x"}, _json_record("ok")])
    assert r.total == 2 and r.imported == 1 and r.skipped == 1
    assert r.errors and "missing task_id" in r.errors[0]


def test_import_json_dir_repeatable_with_backup(store, tmp_path):
    src = tmp_path / "json_tasks"
    src.mkdir()
    (src / "t1.json").write_text(json.dumps(_json_record("t1")), encoding="utf-8")
    (src / "t2.json").write_text(json.dumps(_json_record("t2")), encoding="utf-8")

    r1 = store.import_json_dir(src, backup=True)
    assert r1.imported == 2
    backup = Path(r1.backup_dir)
    assert backup.is_dir()
    assert sorted(p.name for p in backup.glob("*.json")) == ["t1.json", "t2.json"]

    # 再导入：幂等，无重复；备份目录已存在则不重复拷贝
    r2 = store.import_json_dir(src, backup=True)
    assert r2.imported == 2
    assert r2.backup_dir == r1.backup_dir
    assert len(store.list_tasks()) == 2


def test_import_json_dir_missing(store, tmp_path):
    r = store.import_json_dir(tmp_path / "nope")
    assert r.imported == 0 and r.errors


def test_default_journal_path():
    assert default_journal_path("/w").replace("\\", "/") == "/w/runtime.sqlite3"


# ---- open_journal：rollout 开关装配 ----

class _Cfg:
    """轻量配置桩：仅 open_journal 读取的字段。"""
    def __init__(self, enabled=False, work_dir="", journal_path="",
                 max_active=1):
        self.durable_journal_enabled = enabled
        self.work_dir = work_dir
        self.journal_path = journal_path
        self.max_active_executions = max_active


def test_open_journal_disabled_returns_none(tmp_path):
    """开关关闭（默认）→ None，且不创建任何 SQLite 文件（零行为变化）。"""
    cfg = _Cfg(enabled=False, work_dir=str(tmp_path / "work"))
    assert open_journal(cfg) is None
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_open_journal_enabled_creates_and_imports(tmp_path):
    """开关开启→打开 RunStore（派生路径）、启动对账、幂等投影历史 JSON。"""
    work = tmp_path / "work"
    src = tmp_path / "json_tasks"
    src.mkdir()
    (src / "t1.json").write_text(json.dumps(_json_record("t1")), encoding="utf-8")
    cfg = _Cfg(enabled=True, work_dir=str(work))
    store = open_journal(cfg, json_import_dir=src)
    try:
        assert store is not None
        assert Path(default_journal_path(work)).is_file()
        # 历史 JSON 已投影进来（保留供回滚/导出）
        assert store.get_task("t1") is not None
    finally:
        store.close()


def test_open_journal_explicit_path_and_max_active(tmp_path):
    jp = tmp_path / "custom" / "j.sqlite3"
    cfg = _Cfg(enabled=True, journal_path=str(jp), max_active=3)
    store = open_journal(cfg)
    try:
        assert Path(jp).is_file()
        assert store._max_active == 3
    finally:
        store.close()


def test_open_journal_reconciles_interrupted(tmp_path):
    """开启时启动即对账：上个进程遗留的在飞尝试 → interrupted。"""
    work = tmp_path / "work"
    cfg = _Cfg(enabled=True, work_dir=str(work))
    s1 = open_journal(cfg)
    s1.create_task("t1", "r", "p", workspace_key="w")
    s1.try_claim("t1", workspace_key="w", worker="local")  # 在飞，未收尾
    s1.close()  # 模拟进程退出
    # 重启：新 open_journal 对账遗留尝试
    s2 = open_journal(cfg)
    try:
        assert s2.get_task("t1")["status"] == "interrupted"
        assert s2.get_attempt(s2.list_attempts("t1")[0]["attempt_id"])["status"] \
            == "interrupted"
    finally:
        s2.close()


def test_open_journal_degrades_on_error(tmp_path):
    """日志打开失败（如路径不可写）→ 降级返回 None，不抛异常。"""
    cfg = _Cfg(enabled=True, work_dir=str(tmp_path / "work"))
    # journal_path 指向一个无法作为 SQLite 打开的路径（已存在的目录）
    bad = tmp_path / "adir"
    bad.mkdir()
    cfg.journal_path = str(bad)   # 目录作为 db_path → 打开失败
    assert open_journal(cfg) is None
