"""测试 tool_policy：动作分类、审批绑定/复检/过期/参数漂移、副作用账本与
崩溃后重放分类、写后读缓存失效。

核心门（对应方案 Slice 3）：审批绑定到 (action_id, args_hash, resource, attempt)
且带过期；执行前与 resume 后都复检；参数漂移/过期/换 attempt 一律须重新申请；
external 结果未知绝不自动重放，read/idempotent 才可重放。
"""
import pytest

from agent_core.run_store import RunStore
from agent_core.tool_policy import (ActionIdentity, PolicyDecision, ToolPolicy,
                                    ToolPolicyConfig)


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runtime.sqlite3", max_active=1)
    yield s
    s.close()


@pytest.fixture
def policy(store):
    return ToolPolicy(store)


# ---- 分类 ----

def test_classify_read(policy):
    ident = policy.classify("read_file", {"path": "/w/a.txt"})
    assert ident.kind == "read"
    assert ident.requires_approval is False
    assert ident.replay_safe is True
    assert ident.resource == "/w/a.txt"


def test_classify_write_is_external_not_approval(policy):
    # 普通写：保守归 external（结果未知不自动重放），但默认不强制审批。
    ident = policy.classify("write_file", {"path": "/w/a.txt", "content": "x"})
    assert ident.kind == "external"
    assert ident.requires_approval is False
    assert ident.replay_safe is False


def test_classify_delete_requires_approval(policy):
    ident = policy.classify("delete_file", {"path": "/w/a.txt"})
    assert ident.kind == "external"
    assert ident.requires_approval is True


def test_classify_run_and_send_require_approval(policy):
    assert policy.classify("run_shell", {"cmd": "ls"}).requires_approval is True
    assert policy.classify("send_email", {"to": "a@b"}).requires_approval is True


def test_classify_unknown_defaults_external(policy):
    ident = policy.classify("mystery_tool", {"x": 1})
    assert ident.kind == "external"
    assert ident.replay_safe is False


def test_classify_idempotent_override(store):
    cfg = ToolPolicyConfig(idempotent_tools=frozenset({"upsert_by_id"}))
    pol = ToolPolicy(store, cfg)
    ident = pol.classify("upsert_by_id", {"id": 1})
    assert ident.kind == "idempotent"
    assert ident.replay_safe is True


def test_args_hash_ignores_volatile_and_order(policy):
    a = policy.classify("write_file", {"path": "/w/a", "content": "x", "ts": 111})
    b = policy.classify("write_file", {"content": "x", "path": "/w/a", "ts": 999})
    # ts 是易变键被剔除；键顺序不影响哈希 → 同一逻辑动作稳定绑定。
    assert a.args_hash == b.args_hash


def test_args_hash_differs_on_content(policy):
    a = policy.classify("write_file", {"path": "/w/a", "content": "x"})
    b = policy.classify("write_file", {"path": "/w/a", "content": "y"})
    assert a.args_hash != b.args_hash


# ---- 授权：无需审批直接放行 ----

def test_authorize_read_allowed(policy):
    d = policy.authorize("read_file", {"path": "/w/a"}, attempt_id="att1")
    assert d.allowed is True and d.reason == "no_approval_needed"


def test_authorize_write_allowed_without_approval(policy):
    d = policy.authorize("write_file", {"path": "/w/a"}, attempt_id="att1")
    assert d.allowed is True and d.reason == "no_approval_needed"


# ---- 授权：需审批的动作 ----

def test_authorize_approval_required_when_none(policy):
    d = policy.authorize("delete_file", {"path": "/w/a"}, attempt_id="att1")
    assert d.allowed is False and d.reason == "approval_required"


def test_authorize_approved_after_decision(policy):
    args = {"path": "/w/a"}
    policy.decide_approval("delete_file", args, attempt_id="att1",
                           decision="approved", decided_by="human")
    d = policy.authorize("delete_file", args, attempt_id="att1")
    assert d.allowed is True and d.reason == "approved"
    assert d.approval_id is not None


def test_authorize_denied(policy):
    args = {"path": "/w/a"}
    policy.decide_approval("delete_file", args, attempt_id="att1", decision="denied")
    d = policy.authorize("delete_file", args, attempt_id="att1")
    assert d.allowed is False and d.reason == "approval_denied"


def test_authorize_pending(policy):
    args = {"path": "/w/a"}
    policy.request_approval("delete_file", args, attempt_id="att1")
    d = policy.authorize("delete_file", args, attempt_id="att1")
    assert d.allowed is False and d.reason == "approval_pending"


def test_authorize_expired(policy):
    clock = {"t": 1000.0}
    pol = ToolPolicy(policy._store)
    args = {"path": "/w/a"}
    # ttl=10 → 在 t=1000 批准，t=1010 过期。
    pol.decide_approval("delete_file", args, attempt_id="att1",
                        decision="approved", ttl=10, now=clock["t"])
    assert pol.authorize("delete_file", args, attempt_id="att1",
                         now=clock["t"]).allowed is True
    d = pol.authorize("delete_file", args, attempt_id="att1", now=clock["t"] + 11)
    assert d.allowed is False and d.reason == "approval_expired"


def test_authorize_args_changed_not_reusable(policy):
    # 对 path=/w/a 的批准不可复用到 path=/w/b（参数漂移）。
    policy.decide_approval("delete_file", {"path": "/w/a"}, attempt_id="att1",
                           decision="approved")
    d = policy.authorize("delete_file", {"path": "/w/b"}, attempt_id="att1")
    assert d.allowed is False and d.reason == "approval_args_changed"


def test_authorize_recheck_after_resume_new_attempt(policy):
    # resume 后复检：换新 attempt → 旧 attempt 的批准失效，须重新申请。
    args = {"path": "/w/a"}
    policy.decide_approval("delete_file", args, attempt_id="att1",
                           decision="approved")
    assert policy.authorize("delete_file", args, attempt_id="att1").allowed is True
    d = policy.authorize("delete_file", args, attempt_id="att2")
    assert d.allowed is False and d.reason == "approval_required"


# ---- 副作用账本 ----

def test_begin_effect_read_returns_none(policy):
    ident = policy.classify("read_file", {"path": "/w/a"})
    assert policy.begin_effect(ident, task_id="t1", attempt_id="att1") is None


def test_begin_and_commit_effect_invalidates_cache(store):
    invalidated = []
    pol = ToolPolicy(store, cache_invalidator=lambda p: invalidated.append(p) or 1)
    ident = pol.classify("write_file", {"path": "/w/a", "content": "x"})
    eid = pol.begin_effect(ident, task_id="t1", attempt_id="att1")
    assert eid is not None
    assert store.get_effect(eid)["state"] == "started"
    assert pol.commit_effect(eid, {"ok": True}) is True
    assert store.get_effect(eid)["state"] == "committed"
    assert invalidated == [None]   # 写后触发了读缓存失效


def test_begin_and_fail_effect(store):
    pol = ToolPolicy(store, cache_invalidator=lambda p: 0)
    ident = pol.classify("write_file", {"path": "/w/a"})
    eid = pol.begin_effect(ident, task_id="t1", attempt_id="att1")
    assert pol.fail_effect(eid, {"error": "boom"}) is True
    assert store.get_effect(eid)["state"] == "failed"


def test_commit_none_effect_is_noop(policy):
    assert policy.commit_effect(None) is False
    assert policy.fail_effect(None) is False


# ---- 崩溃后重放分类 ----

def test_replay_plan_separates_safe_and_unknown(store):
    pol = ToolPolicy(store, cache_invalidator=lambda p: 0)
    # 一个显式幂等动作（可重放）+ 一个 external 动作（结果未知）。
    idem = pol.classify("upsert_by_id", {"id": 1})
    store.record_effect_start(idem.action_id, "idempotent",
                              args_hash=idem.args_hash, task_id="t1")
    ext = pol.classify("send_email", {"to": "a@b"})
    store.record_effect_start(ext.action_id, "external",
                              args_hash=ext.args_hash, task_id="t1")
    plan = pol.replay_plan(task_id="t1")
    assert len(plan["replay_safe"]) == 1
    assert len(plan["needs_reconciliation"]) == 1
    # external 未决 → 被落为 unknown 挂起，绝不自动重放。
    hold_id = plan["needs_reconciliation"][0]
    assert store.get_effect(hold_id)["state"] == "unknown"


def test_replay_plan_empty_when_all_committed(store):
    pol = ToolPolicy(store, cache_invalidator=lambda p: 0)
    ident = pol.classify("write_file", {"path": "/w/a"})
    eid = pol.begin_effect(ident, task_id="t1", attempt_id="att1")
    pol.commit_effect(eid)
    plan = pol.replay_plan(task_id="t1")
    assert plan == {"replay_safe": [], "needs_reconciliation": []}


# ---- ActionIdentity 属性 ----

def test_action_identity_replay_safe():
    assert ActionIdentity("a", "h", "r", "read", False).replay_safe is True
    assert ActionIdentity("a", "h", "r", "idempotent", False).replay_safe is True
    assert ActionIdentity("a", "h", "r", "external", True).replay_safe is False


# ---- tool_guard 读缓存失效（默认 invalidator 走的真实路径）----

def test_tool_guard_cache_invalidate_prefix_and_all():
    from agent_core import tool_guard
    cache = tool_guard._ToolCache()
    cache.put("read_file:h1", "v1")
    cache.put("read_file:h2", "v2")
    cache.put("list_dir:h3", "v3")
    # 定向失效：只清 read_file: 前缀
    assert cache.invalidate("read_file:") == 2
    assert cache.get("read_file:h1") is None
    assert cache.get("list_dir:h3") == "v3"
    # 全量失效
    assert cache.invalidate() == 1
    assert cache.get("list_dir:h3") is None


def test_default_invalidator_clears_shared_cache(store):
    from agent_core import tool_guard
    # 写入共享缓存，提交一个写副作用（用默认 invalidator）后应被清空。
    tool_guard._tool_cache.put("read_file:hX", "stale")
    pol = ToolPolicy(store)   # 不注入 → 用默认 tool_guard 失效器
    ident = pol.classify("write_file", {"path": "/w/a"})
    eid = pol.begin_effect(ident, task_id="t1", attempt_id="att1")
    assert pol.commit_effect(eid) is True
    assert tool_guard._tool_cache.get("read_file:hX") is None
