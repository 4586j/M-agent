"""本地执行派发器：在 brain 进程内直接运行 AgentKernel。

使 brain local 模式 + ORCH_LOCAL_EXEC=1 时 /dispatch 端点可真正执行任务，
而非返回"未配置"。单机 = brain_api(local) + LocalExecutorDispatcher + AgentKernel。
阶段三可观测性：生成 trace_id、将 kernel 用量/成本写入 TaskRecord.meta、
向事件总线发布状态迁移（SSE 实时流复用远程派发同一套事件语义）。

与 RemoteDispatcher 的区别：不选 worker、不发 HTTP、不轮询——直接在本进程用
``AgentKernel.run_oneshot()`` 同步跑完，再走与远程派发一致的收尾（写 TaskStore、
记经验、回写上下文记忆、产物裁决）。role 即人设名（executor / security-guard）。
"""
import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

# 确保项目根可导入 agent_core：runtime → orchestrator → agent → workspace → 项目根
_RUNTIME_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _RUNTIME_DIR.parents[3]
for _p in (str(_RUNTIME_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.config import AgentConfig, load_agent_config  # noqa: E402
from agent_core.kernel import AgentKernel  # noqa: E402
from agent_core.persona import Persona, load_persona_by_name  # noqa: E402
from agent_core.run_store import RunStore, open_journal  # noqa: E402

from dispatch_common import (USAGE_KEYS, is_text_artifact, enrich_experience,
                             write_result_note, put_provider_name)  # noqa: E402
from dispatcher import Dispatcher, DispatchResult  # noqa: E402
from event_bus import TaskEventBus, publish_task_event  # noqa: E402
from experience_store import ExperienceStore  # noqa: E402
from memory_store import MemoryStore  # noqa: E402
from task_store import TaskRecord, TaskStore, new_trace_id  # noqa: E402
from dispatch_context import new_dispatch_id
from cost_accounting import capture_cost

log = logging.getLogger(__name__)

def _kernel_meta(result: Any) -> dict:
    """KernelResult -> 可观测 meta（与 worker 侧 agent_runner 同一映射口径）。

    内核产出的 result.meta（degenerate 输出质量标记、reflection、plan 状态等）
    一并并入，经 TaskRecord.meta 入库，供 /stats quality 聚合与重试消费。
    """
    meta: dict = {"num_turns": result.turns}
    if result.usage:
        meta["usage"] = {k: result.usage.get(k) for k in USAGE_KEYS
                         if k in result.usage}
    if result.cost_usd is not None:
        meta["cost_usd"] = result.cost_usd
    if result.duration_ms:
        meta["duration_ms"] = result.duration_ms
    if getattr(result, "meta", None):
        meta.update(result.meta)
    return meta


class LocalExecutorDispatcher(Dispatcher):
    """本地执行派发器：在 brain 进程内直接运行 AgentKernel。

    构造参数：
    - task_store: 任务持久化
    - config: AgentConfig（缺省则 load_agent_config()）
    - experience_store / memory_store: 可选，用于经验记录与上下文记忆回写
    - product_check: 可选，Callable[[str], dict]，对产物做 security-guard 裁决
    - persona_dir: 可选，人设定义目录（缺省用 config.persona_dir）
    """

    supports_budget_accounting = True

    def __init__(self, task_store: TaskStore, config: AgentConfig | None = None,
                 experience_store: ExperienceStore | None = None,
                 memory_store: MemoryStore | None = None,
                 product_check: Callable[[str], dict] | None = None,
                 persona_dir: str | None = None,
                 now_fn: Callable[[], str] | None = None,
                 check_max_chars: int = 6000,
                 event_bus: TaskEventBus | None = None,
                 journal: RunStore | None = None,
                 on_spend: Callable[[float], None] | None = None):
        self.task_store = task_store
        self.config = config or load_agent_config()
        self.experience_store = experience_store
        self.memory_store = memory_store
        self.product_check = product_check
        self.persona_dir = persona_dir or self.config.persona_dir
        self._now = now_fn or (lambda: str(int(time.time())))
        self.check_max_chars = check_max_chars
        self.event_bus = event_bus
        self.on_spend = on_spend  # 预算记账钩子：任务终态时上报 cost_usd（可 None）
        self._kernel = AgentKernel(self.config)
        self._persona_cache: dict[str, Persona] = {}
        # 在飞任务的协作式取消事件（P0：本地取消须真正停掉本地执行）。
        # cancel_task 端点与本派发线程分属不同线程，故用锁保护注册表。
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        # 持久化执行日志（Slice 3 rollout flag）：显式注入优先；否则按
        # config.durable_journal_enabled 装配（关闭时 open_journal 返回 None，
        # 完全走既有 JSON 权威路径，零行为变化）。开启后作为本地执行的持久
        # 恢复底座：有界准入 + 尝试谱系 + 启动对账；JSON 保留供回滚/导出。
        self.journal = journal if journal is not None else open_journal(
            self.config,
            work_dir=(self.config.work_dir
                      or (self.config.project_root or ".") + "/.agent_work"),
            json_import_dir=getattr(task_store, "store_dir", None))

    def recover_orphans(self) -> int:
        """启动恢复：把上次进程遗留的 running/pending/dispatching 任务标记 failed。

        本地执行体与 brain 同进程，brain 重启即执行体消亡——遗留的「进行中」
        任务永远不会有结果，挂着比失败更误导。标记为 failed（error 注明原因），
        可经重派端点重新执行。返回受影响任务数。仅在 brain 装配（_build_app）
        时、开始服务之前调用，不会误标本进程新派发的任务。
        """
        affected = 0
        for rec in self.task_store.list_all():
            if rec.status not in ("running", "pending", "dispatching"):
                continue
            original = rec.status  # update 会原地改 rec.status，先留档供日志
            self.task_store.update(
                rec.task_id, status="failed",
                error="brain 重启：本地执行进程中断，任务未完成（可重派）")
            affected += 1
            log.warning("[trace=%s] 启动恢复：孤儿任务 task=%s（原状态 %s）标记 failed",
                        rec.trace_id, rec.task_id, original)
        return affected

    def cancel(self, task_id: str) -> bool:
        """信号本地执行协作式取消：置位在飞任务的 cancel_event。

        返回是否命中一个正在本地执行的任务（已终态/非本地任务返回 False）。
        仅发出协作式中断信号（停在 turn 边界）；终态写入由 cancel 端点与
        派发收尾的 finalize_if 共同保证不被覆盖。
        """
        with self._cancel_lock:
            ev = self._cancel_events.get(task_id)
        if ev is None:
            return False
        ev.set()
        log.info("local exec cancel signaled task=%s", task_id)
        return True

    # ---- 持久化日志：有界准入 + 尝试谱系（仅在 rollout flag 开启时生效）----

    def _journal_workspace_key(self) -> str:
        """按共享工作区串行化的键（同工作区一次只一个在飞尝试）。"""
        return self.config.work_dir or (self.config.project_root or ".")

    def _journal_admit(self, task_id: str, role: str, prompt: str,
                       trace_id: str) -> tuple[bool, str | None, str]:
        """在持久化日志登记任务并做有界准入。返回 (admitted, attempt_id, reason)。

        饱和/串行化冲突 → admitted=False（上层显式拒绝，不静默排队）。
        日志写入异常 → 降级为放行（JSON 仍权威），reason 带 journal_error 告警。
        """
        j = self.journal
        wkey = self._journal_workspace_key()
        try:
            if j.get_task(task_id) is None:
                j.create_task(task_id, role, prompt, workspace_key=wkey,
                              status="pending", trace_id=trace_id)
            claim = j.try_claim(task_id, workspace_key=wkey, worker="local")
            return claim.admitted, claim.attempt_id, claim.reason
        except Exception as e:  # noqa: BLE001 - 日志故障不阻断执行
            log.warning("journal admit failed task=%s (degrade to JSON): %r",
                        task_id, e)
            return True, None, f"journal_error:{e}"

    def _journal_finish(self, attempt_id: str | None, task_id: str,
                        status: str, meta: dict) -> None:
        """把终态回写日志（任务 CAS + 尝试收尾）。异常仅告警，不拖垮派发。"""
        j = self.journal
        terminal = status if status in ("done", "failed", "cancelled") else "failed"
        try:
            j.cas_task_status(task_id,
                              expected=("running", "pending", "interrupted"),
                              to_status=terminal, meta=meta)
            if attempt_id:
                j.finish_attempt(attempt_id, terminal, result={"status": status})
        except Exception as e:  # noqa: BLE001
            log.warning("journal finish failed task=%s: %r", task_id, e)

    # ---- 人设与工作目录 ----

    def _resolve_persona(self, role: str, model: str | None = None,
                         provider: dict | None = None) -> Persona:
        """role → 人设。找不到定义文件时给最小人设兜底；model/本地 provider 非空则覆盖。"""
        persona = self._persona_cache.get(role)
        if persona is None:
            try:
                persona = load_persona_by_name(role, self.persona_dir)
            except FileNotFoundError:
                persona = Persona(name=role,
                                  description=f"{role}（未找到定义文件，使用最小人设）",
                                  system_prompt=f"你是 {role}。请按任务说明完成工作。")
            self._persona_cache[role] = persona
        # 不改缓存原件，复制一份覆盖（dataclass 不可变语义更安全）
        from agent_core.persona import replace_persona
        overrides: dict = {}
        if model:
            overrides["model"] = model
        if provider and provider.get("name"):
            overrides["provider"] = provider.get("name")
        # 本地执行直接进程内跑内核：base_url/auth_token 经 persona 注入
        # _build_options 的 options.env（不落盘）。
        if provider:
            if provider.get("base_url"):
                overrides["base_url"] = provider["base_url"]
            if provider.get("auth_token"):
                overrides["auth_token"] = provider["auth_token"]
        if overrides:
            return replace_persona(persona, **overrides)
        return persona

    def _prepare_cwd(self, task_id: str) -> str:
        """为任务开一个独立工作目录（产物落地处），返回绝对路径字符串。"""
        base = Path(self.config.work_dir
                    or (self.config.project_root or ".") + "/.agent_work")
        cwd = base / task_id
        cwd.mkdir(parents=True, exist_ok=True)
        return str(cwd)

    def _collect_product_text(self, cwd: str, output: Any,
                              artifacts: list) -> str:
        """合并 output 与文本 artifacts 内容供产物裁决；截断到 check_max_chars。"""
        combined = str(output or "")
        cwd_path = Path(cwd)
        for art in artifacts or []:
            if not is_text_artifact(str(art)):
                continue
            fp = cwd_path / str(art)
            if not fp.is_file():
                continue
            try:
                combined += f"\n\n--- artifact: {art} ---\n" + fp.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - 单个产物读取失败不拖垮裁决
                continue
        return combined[:self.check_max_chars]

    # ---- 派发主流程 ----

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None,
                 model: str | None = None,
                 use_experience: bool = True,
                 enable_subagents: bool | None = None,
                 worker_id: str | None = None,
                 provider: dict | None = None) -> DispatchResult:
        """本地同步执行一个任务：写 pending → run_oneshot → 收尾更新。

        use_experience=False 为评测消融组的硬关：不检索/不注入历史经验（读关），
        且内核与派发层均不沉淀经验（写关）——真正禁用读写，而非仅靠提示词。
        enable_subagents：每次请求覆盖子代理派发开关；None=跟随全局
        config.subagents_enabled（AGENT_/ORCH_SUBAGENTS_ENABLED）。
        worker_id：本地执行无 worker 概念，只接受 None 或伪定点 "local"，
        其余一律报错（不区分角色，本地全部进程内执行）。
        """
        if worker_id is not None and worker_id != "local":
            raise RuntimeError(
                f"本地执行不支持定点节点 {worker_id}（进程内直跑，没有独立 worker）。"
                "单机本地执行请省略 worker_id，或在 ORCH_MODE=remote 下注册 worker 后按节点派发。")
        task_id = new_dispatch_id()
        trace_id = new_trace_id()
        ts = self._now()

        # 持久化日志有界准入（rollout flag 开启时）：饱和/串行化冲突则显式拒绝，
        # 不静默排队、不创建 JSON 任务与工作目录。关闭时 journal=None，整段跳过。
        attempt_id = None
        if self.journal is not None:
            admitted, attempt_id, reason = self._journal_admit(
                task_id, role, prompt, trace_id)
            if not admitted:
                log.warning("[trace=%s] journal admission rejected task=%s: %s",
                            trace_id, task_id, reason)
                return DispatchResult(task_id=task_id, status="rejected",
                                      output=None, artifacts=[], error=reason,
                                      verdict=None, trace_id=trace_id)

        cwd = self._prepare_cwd(task_id)

        meta: dict = {}
        if model:
            meta["model"] = model
        meta["use_experience"] = bool(use_experience)  # 与 sync/async 统一口径
        put_provider_name(meta, provider)
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="pending", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None,
            trace_id=trace_id, meta=meta,
        ))
        log.info("[trace=%s] local exec dispatch role=%s task=%s", trace_id, role, task_id)
        publish_task_event(self.event_bus, task_id, event="status",
                           state="pending", trace_id=trace_id)
        self.task_store.update(task_id, status="running")
        publish_task_event(self.event_bus, task_id, event="status",
                           state="running", trace_id=trace_id)

        # 协作式取消：为本任务登记 cancel_event 并传入内核，使 cancel 端点
        # 能真正停掉本地执行（P0：此前本地执行收不到任何取消信号）。
        cancel_event = threading.Event()
        with self._cancel_lock:
            self._cancel_events[task_id] = cancel_event
        try:
            persona = self._resolve_persona(role, model=model, provider=provider)
            # 消融硬关（读）：use_experience=False 时跳过经验检索/注入。
            enriched_ctx = (enrich_experience(self.experience_store, prompt, role,
                                              context_memory)
                            if use_experience else (context_memory or ""))
            # 子代理派发每次请求覆盖：以替换配置的一次性内核实现（race-free 局部变量），
            # 双向覆盖全局默认（kernel._build_options 读 config.subagents_enabled）。
            kernel = self._kernel
            if enable_subagents is not None:
                eff = bool(enable_subagents)
                if eff != bool(getattr(self.config, "subagents_enabled", True)):
                    kernel = AgentKernel(replace(self.config, subagents_enabled=eff))
            result = kernel.run_oneshot(prompt, persona, cwd,
                                        context_memory=enriched_ctx or "",
                                        cancel_event=cancel_event,
                                        record_experience=use_experience)
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(task_id, None)

        # 取消如实归类：内核观察到 cancel_event 则 result.cancelled=True，
        # 绝不把被取消的执行谎报为 done。
        if result.cancelled:
            status = "cancelled"
        else:
            status = "failed" if result.is_error else "done"
        output = result.output
        artifacts = list(result.artifacts or [])
        error = output if result.is_error else None
        managed_cost = capture_cost(self, task_id, _kernel_meta(result))

        verdict = None
        if status == "done" and self.product_check:
            verdict = self.product_check(
                self._collect_product_text(cwd, output, artifacts))

        # kernel 用量/成本并入 TaskRecord.meta；model/use_experience/provider 名与
        # 初始 meta 同口径（此前 local 终态会丢 provider 名，与 sync/async 不一致）
        meta = {"model": model} if model else {}
        meta["use_experience"] = bool(use_experience)
        put_provider_name(meta, provider)
        meta.update(_kernel_meta(result))
        # 预算记账：内核真实花费（含被取消任务的已消耗部分）上报账本
        if self.on_spend is not None and not managed_cost:
            cost = meta.get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) \
                    and cost > 0:
                try:
                    self.on_spend(float(cost))
                except Exception:  # noqa: BLE001 - 记账失败不影响收尾
                    log.warning("预算记账失败 cost=%s: %r", cost, exc_info=True)
        # 取消不可覆盖（P0）：仅当任务仍处运行态才写入终态；若 cancel 端点
        # 已置 cancelled（终态、不可变），迟到的收尾写入不得覆盖它。
        final = self.task_store.finalize_if(
            task_id, expected=("running", "pending"),
            status=status, output=output, artifacts=artifacts, error=error,
            verdict=verdict, meta=meta)
        if final is None:
            # 已被取消/已终态化：以 store 里的终态为准，不重复发 done 事件
            # （cancel 端点已发布），也不沉淀经验。
            cur = self.task_store.get(task_id)
            status = cur.status if cur else "cancelled"
            log.info("[trace=%s] task=%s finalize skipped (already terminal=%s)",
                     trace_id, task_id, status)
            if self.journal is not None:
                self._journal_finish(attempt_id, task_id, status, meta)
            return DispatchResult(task_id=task_id, status=status, output=output,
                                  artifacts=artifacts, error=error, verdict=verdict,
                                  trace_id=trace_id)

        log.info("[trace=%s] task=%s terminal status=%s", trace_id, task_id, status)
        publish_task_event(self.event_bus, task_id, event="done", state=status,
                           trace_id=trace_id,
                           summary=output if output is not None else error)

        # 取消的任务不沉淀经验（未真实完成）；done/failed 照常记录。
        # 消融硬关（写）：use_experience=False 时派发层也不沉淀。
        if self.experience_store and use_experience and status in ("done", "failed"):
            try:
                self.experience_store.record_from_task(
                    task_id=task_id, role=role, prompt=prompt, output=output,
                    success=(status == "done"), verdict=verdict,
                    artifacts=artifacts, created_at=ts,
                )
            except Exception:  # noqa: BLE001 - 经验记录失败不拖垮派发
                pass
        write_result_note(self.memory_store, task_id, status, output, error)

        if self.journal is not None:
            self._journal_finish(attempt_id, task_id, status, meta)

        return DispatchResult(task_id=task_id, status=status, output=output,
                              artifacts=artifacts, error=error, verdict=verdict,
                              trace_id=trace_id)

    def redispatch(self, task_id: str, model: str | None = None,
                   worker_id: str | None = None,
                   provider: dict | None = None) -> DispatchResult:
        """按已存在任务的 role/prompt 重新本地执行，返回新 DispatchResult。

        provider 签名与基类/远程派发器对齐（此前缺失：脑 /redispatch 带
        provider 重派本地任务会 TypeError）。
        """
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model,
                             worker_id=worker_id, provider=provider)
