"""工具调用策略层（pre-tool policy）。

在 run_store 的审批/副作用原语之上，落实可靠性方案 Slice 3「Tool policy」：

- **动作分类**：把每次工具调用归类为 read / idempotent / external，决定其重放安全性。
  read 与「显式声明的」idempotent 才可重放；其余（含普通写、删除、执行、外发、
  未知工具）一律 external —— 结果未知时挂起对账，绝不盲目自动重放。
- **审批绑定**：审批绑定到 (action_id, 归一化 args_hash, resource, attempt, expiry)。
  执行前复检、resume 后复检（换 attempt 即失效，须重新申请）；参数漂移 / 过期 /
  被拒 / 待裁决各自给出精确原因，而非笼统「未授权」。
- **副作用账本**：external/idempotent 动作执行前登记 started、成功后 committed、
  异常 failed；崩溃停留在 started 的，重启对账把非 read/idempotent 者标记 unknown
  挂起，read/idempotent 者列为可安全重放。
- **缓存作用域**：只缓存 scoped read 结果；写动作提交后使相关读缓存失效。

本层不依赖具体 SDK：classify/authorize 为纯决策函数，副作用与审批经注入的
RunStore 落地，缓存失效经注入的 invalidator 落地（默认走 tool_guard 的进程内缓存）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_core.run_store import RunStore

log = logging.getLogger(__name__)


def _default_cache_invalidator(prefix: str | None = None) -> int:
    """默认缓存失效器：惰性引用 tool_guard 的进程内共享读缓存。"""
    from agent_core.tool_guard import invalidate_tool_cache
    return invalidate_tool_cache(prefix)


@dataclass(frozen=True)
class ToolPolicyConfig:
    """工具策略配置（分类前缀 / 审批面 / 归一化 / 资源键）。

    分类采用「保守默认」：未识别为 read 的动作一律 external（结果未知不重放）；
    idempotent 必须显式声明（read_tools/idempotent_tools/kind_overrides）。
    审批面（requires_approval）与重放分类（kind）是两条独立的轴：普通写默认
    kind=external 但不强制审批；删除/执行/外发等破坏性或结果未知动作默认需审批。
    """
    read_prefixes: tuple[str, ...] = (
        "read_", "get_", "list_", "search_", "view_", "fetch_", "show_",
        "cat_", "find_", "grep_", "glob_",
    )
    write_prefixes: tuple[str, ...] = (
        "write_", "create_", "update_", "edit_", "mkdir_", "move_", "rename_",
        "append_", "put_", "set_", "save_",
    )
    external_prefixes: tuple[str, ...] = (
        "delete_", "remove_", "rm_", "run_", "exec_", "send_", "post_",
        "bash", "shell", "terminate_", "kill_", "push_", "publish_",
    )
    # 需要人工审批的前缀（破坏性 / 外发 / 结果未知）。
    approval_prefixes: tuple[str, ...] = (
        "delete_", "remove_", "rm_", "run_", "exec_", "send_", "post_",
        "bash", "shell", "terminate_", "kill_", "push_", "publish_",
    )
    read_tools: frozenset[str] = frozenset()
    idempotent_tools: frozenset[str] = frozenset()   # 显式声明幂等（可重放）
    approval_tools: frozenset[str] = frozenset()      # 显式要求审批
    no_approval_tools: frozenset[str] = frozenset()   # 显式豁免审批
    kind_overrides: dict[str, str] = field(default_factory=dict)
    # 归一化时剔除的易变键（时间戳/请求 id 等）——避免同一逻辑动作因易变字段
    # 产生不同 args_hash 而无法复用审批。
    volatile_arg_keys: frozenset[str] = frozenset({
        "request_id", "req_id", "timestamp", "ts", "nonce", "now", "trace_id",
        "idempotency_key", "_ts",
    })
    # 目标资源提取键（用于把审批绑定到具体资源）。
    resource_arg_keys: tuple[str, ...] = (
        "path", "file_path", "filepath", "target", "url", "resource", "dest",
        "destination", "filename", "name", "cmd", "command",
    )
    default_kind: str = "external"
    require_approval_default: bool = False


@dataclass(frozen=True)
class ActionIdentity:
    """一次工具调用的策略身份：动作 + 归一化参数 + 目标资源 + 分类。"""
    action_id: str
    args_hash: str
    resource: str
    kind: str                    # read | idempotent | external
    requires_approval: bool

    @property
    def replay_safe(self) -> bool:
        return self.kind in ("read", "idempotent")


@dataclass
class PolicyDecision:
    """授权决策。allowed=False 时 reason 给出精确原因（供上层重新申请/挂起）。"""
    allowed: bool
    reason: str
    action: ActionIdentity
    approval_id: str | None = None


class ToolPolicy:
    """工具调用策略：分类 → 授权（审批绑定/复检）→ 副作用账本 → 缓存失效。

    参数：
    - store: RunStore（审批与副作用的持久化底座）。
    - config: ToolPolicyConfig（分类/审批/归一化规则）。
    - cache_invalidator: 可注入的读缓存失效器 (prefix|None) -> int，默认走 tool_guard。
    """

    def __init__(self, store: RunStore, config: ToolPolicyConfig | None = None, *,
                 cache_invalidator: Callable[[str | None], int] | None = None):
        self._store = store
        self.config = config or ToolPolicyConfig()
        self._invalidate_cache = cache_invalidator or _default_cache_invalidator

    # ---- 分类 ----

    def _classify_kind(self, tool_name: str) -> str:
        cfg = self.config
        if tool_name in cfg.kind_overrides:
            return cfg.kind_overrides[tool_name]
        if tool_name in cfg.idempotent_tools:
            return "idempotent"
        if tool_name in cfg.read_tools:
            return "read"
        for pfx in cfg.read_prefixes:
            if tool_name.startswith(pfx):
                return "read"
        for pfx in cfg.external_prefixes:
            if tool_name.startswith(pfx):
                return "external"
        for pfx in cfg.write_prefixes:
            if tool_name.startswith(pfx):
                # 普通写：非显式幂等 → 保守归为 external（结果未知不自动重放）。
                return "external"
        return cfg.default_kind

    def _requires_approval(self, tool_name: str, kind: str) -> bool:
        cfg = self.config
        if tool_name in cfg.approval_tools:
            return True
        if tool_name in cfg.no_approval_tools:
            return False
        for pfx in cfg.approval_prefixes:
            if tool_name.startswith(pfx):
                return True
        return cfg.require_approval_default

    def _hash_args(self, args: dict) -> str:
        """归一化参数哈希：剔除易变键、按 key 排序，保证同一逻辑动作稳定绑定。"""
        norm = {k: v for k, v in args.items()
                if k not in self.config.volatile_arg_keys}
        try:
            raw = json.dumps(norm, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = repr(sorted(norm.items()))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _extract_resource(self, args: dict) -> str:
        for k in self.config.resource_arg_keys:
            if k in args and isinstance(args[k], (str, int, float)):
                return str(args[k])
        return ""

    def classify(self, tool_name: str, args: dict | None = None, *,
                 action_id: str | None = None) -> ActionIdentity:
        """把一次工具调用归类为 ActionIdentity（纯函数，无副作用）。"""
        args = args or {}
        kind = self._classify_kind(tool_name)
        return ActionIdentity(
            action_id=action_id or tool_name,
            args_hash=self._hash_args(args),
            resource=self._extract_resource(args),
            kind=kind,
            requires_approval=self._requires_approval(tool_name, kind),
        )

    # ---- 授权：审批绑定 + 执行前/resume 后复检 ----

    def authorize(self, tool_name: str, args: dict | None = None, *,
                  attempt_id: str, task_id: str = "",
                  action_id: str | None = None,
                  now: float | None = None) -> PolicyDecision:
        """执行前授权（同一函数用于 resume 后复检）。

        - 无需审批的动作（read / 非破坏性写）直接放行。
        - 需审批的动作：仅当存在绑定到 (action_id, args_hash, resource, attempt)
          且未过期的 approved 裁决时放行；否则给出精确拒绝原因：
          approval_required（从未申请）/ approval_pending（待裁决）/
          approval_denied（已拒绝）/ approval_expired（已过期）/
          approval_args_changed（同动作同尝试但参数/资源漂移）。
        """
        ident = self.classify(tool_name, args, action_id=action_id)
        if not ident.requires_approval:
            return PolicyDecision(True, "no_approval_needed", ident)

        valid = self._store.get_valid_approval(
            ident.action_id, args_hash=ident.args_hash, resource=ident.resource,
            attempt_id=attempt_id, now=now)
        if valid is not None:
            return PolicyDecision(True, "approved", ident,
                                  approval_id=valid.get("approval_id"))

        exact = self._store.latest_approval(
            ident.action_id, args_hash=ident.args_hash,
            resource=ident.resource, attempt_id=attempt_id)
        if exact is None:
            # 同动作同尝试是否存在「参数不同」的审批 → 参数漂移（审批不可跨参数复用）。
            drifted = self._store.latest_approval(
                ident.action_id, attempt_id=attempt_id)
            if drifted is not None:
                return PolicyDecision(False, "approval_args_changed", ident)
            return PolicyDecision(False, "approval_required", ident)

        decision = exact.get("decision")
        if decision == "pending":
            return PolicyDecision(False, "approval_pending", ident)
        if decision == "denied":
            return PolicyDecision(False, "approval_denied", ident)
        # approved 但不在有效期（get_valid_approval 已过滤过期）→ 过期。
        return PolicyDecision(False, "approval_expired", ident)

    def request_approval(self, tool_name: str, args: dict | None = None, *,
                         attempt_id: str, task_id: str = "",
                         action_id: str | None = None, ttl: float | None = None,
                         decided_by: str = "", now: float | None = None) -> dict:
        """登记一条 pending 审批请求（绑定动作身份 + 可选 ttl 过期），返回审批行。"""
        ident = self.classify(tool_name, args, action_id=action_id)
        return self._store.record_approval(
            ident.action_id, args_hash=ident.args_hash, resource=ident.resource,
            task_id=task_id, attempt_id=attempt_id, decision="pending",
            ttl=ttl, now=now)

    def decide_approval(self, tool_name: str, args: dict | None = None, *,
                        attempt_id: str, task_id: str = "",
                        action_id: str | None = None, decision: str = "approved",
                        decided_by: str = "", ttl: float | None = None,
                        now: float | None = None) -> dict:
        """登记一条裁决（approved/denied），绑定动作身份 + 可选 ttl 过期。"""
        if decision not in ("approved", "denied"):
            raise ValueError(f"decision must be approved/denied, got {decision!r}")
        ident = self.classify(tool_name, args, action_id=action_id)
        return self._store.record_approval(
            ident.action_id, args_hash=ident.args_hash, resource=ident.resource,
            task_id=task_id, attempt_id=attempt_id, decision=decision,
            decided_by=decided_by, ttl=ttl, now=now)

    # ---- 副作用账本：崩溃后不盲目重放 ----

    def begin_effect(self, ident: ActionIdentity, *, task_id: str = "",
                     attempt_id: str = "", now: float | None = None
                     ) -> str | None:
        """执行有副作用的动作「之前」登记 started，返回 effect_id。

        纯 read 动作不登记（无持久副作用，重放天然安全），返回 None。
        """
        if ident.kind == "read":
            return None
        eff = self._store.record_effect_start(
            ident.action_id, ident.kind, args_hash=ident.args_hash,
            task_id=task_id, attempt_id=attempt_id, now=now)
        return eff["effect_id"]

    def commit_effect(self, effect_id: str | None, outcome: Any = None, *,
                      invalidate_prefix: str | None = None,
                      now: float | None = None) -> bool:
        """动作成功 → committed；并使相关读缓存失效（写后不读陈旧快照）。"""
        if not effect_id:
            return False
        ok = self._store.set_effect_state(effect_id, "committed", outcome, now=now)
        if ok:
            try:
                self._invalidate_cache(invalidate_prefix)
            except Exception:  # noqa: BLE001 - 缓存失效失败不阻断提交
                log.warning("cache invalidation failed for effect %s", effect_id)
        return ok

    def fail_effect(self, effect_id: str | None, outcome: Any = None, *,
                    now: float | None = None) -> bool:
        """动作失败 → failed（明确失败与「结果未知」区分：failed 不是 unknown）。"""
        if not effect_id:
            return False
        return self._store.set_effect_state(effect_id, "failed", outcome, now=now)

    def replay_plan(self, *, task_id: str | None = None) -> dict:
        """崩溃/重启后对未决副作用（started/unknown）分类。

        返回 {"replay_safe": [...], "needs_reconciliation": [...]}：
        - read/idempotent → replay_safe（可安全自动重放）。
        - external（结果未知）→ needs_reconciliation，并把 state 落为 unknown 挂起，
          绝不自动重放（对应方案「Ambiguous effects are not replayed」）。
        """
        unresolved = self._store.list_unresolved_effects(task_id=task_id)
        safe: list[str] = []
        hold: list[str] = []
        for eff in unresolved:
            eid = eff["effect_id"]
            if RunStore.is_replay_safe(eff):
                safe.append(eid)
            else:
                if eff.get("state") != "unknown":
                    self._store.set_effect_state(eid, "unknown")
                hold.append(eid)
        return {"replay_safe": safe, "needs_reconciliation": hold}
