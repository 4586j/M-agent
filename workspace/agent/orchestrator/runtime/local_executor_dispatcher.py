"""本地执行派发器：在 brain 进程内直接运行 AgentKernel。

使 brain local 模式 + ORCH_LOCAL_EXEC=1 时 /dispatch 端点可真正执行任务，
而非返回"未配置"。单机 = brain_api(local) + LocalExecutorDispatcher + AgentKernel。

与 RemoteDispatcher 的区别：不选 worker、不发 HTTP、不轮询——直接在本进程用
``AgentKernel.run_oneshot()`` 同步跑完，再走与远程派发一致的收尾（写 TaskStore、
记经验、回写上下文记忆、产物裁决）。role 即人设名（executor / security-guard）。
"""
import sys
import time
import uuid
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

from dispatch_common import is_text_artifact, enrich_experience, write_result_note  # noqa: E402
from dispatcher import Dispatcher, DispatchResult  # noqa: E402
from experience_store import ExperienceStore  # noqa: E402
from memory_store import MemoryStore  # noqa: E402
from task_store import TaskRecord, TaskStore  # noqa: E402


class LocalExecutorDispatcher(Dispatcher):
    """本地执行派发器：在 brain 进程内直接运行 AgentKernel。

    构造参数：
    - task_store: 任务持久化
    - config: AgentConfig（缺省则 load_agent_config()）
    - experience_store / memory_store: 可选，用于经验记录与上下文记忆回写
    - product_check: 可选，Callable[[str], dict]，对产物做 security-guard 裁决
    - persona_dir: 可选，人设定义目录（缺省用 config.persona_dir）
    """

    def __init__(self, task_store: TaskStore, config: AgentConfig | None = None,
                 experience_store: ExperienceStore | None = None,
                 memory_store: MemoryStore | None = None,
                 product_check: Callable[[str], dict] | None = None,
                 persona_dir: str | None = None,
                 now_fn: Callable[[], str] | None = None,
                 check_max_chars: int = 6000):
        self.task_store = task_store
        self.config = config or load_agent_config()
        self.experience_store = experience_store
        self.memory_store = memory_store
        self.product_check = product_check
        self.persona_dir = persona_dir or self.config.persona_dir
        self._now = now_fn or (lambda: str(int(time.time())))
        self.check_max_chars = check_max_chars
        self._kernel = AgentKernel(self.config)
        self._persona_cache: dict[str, Persona] = {}

    # ---- 人设与工作目录 ----

    def _resolve_persona(self, role: str, model: str | None = None) -> Persona:
        """role → 人设。找不到定义文件时给最小人设兜底；model 非空则覆盖。"""
        persona = self._persona_cache.get(role)
        if persona is None:
            try:
                persona = load_persona_by_name(role, self.persona_dir)
            except FileNotFoundError:
                persona = Persona(name=role,
                                  description=f"{role}（未找到定义文件，使用最小人设）",
                                  system_prompt=f"你是 {role}。请按任务说明完成工作。")
            self._persona_cache[role] = persona
        if model:
            # 不改缓存原件，复制一份覆盖 model（dataclass 不可变语义更安全）
            return replace(persona, model=model)
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
                 model: str | None = None) -> DispatchResult:
        """本地同步执行一个任务：写 pending → run_oneshot → 收尾更新。"""
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        cwd = self._prepare_cwd(task_id)

        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="pending", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None,
        ))
        self.task_store.update(task_id, status="running")

        persona = self._resolve_persona(role, model=model)
        enriched_ctx = enrich_experience(self.experience_store, prompt, role,
                                         context_memory)
        result = self._kernel.run_oneshot(prompt, persona, cwd,
                                          context_memory=enriched_ctx or "")

        status = "failed" if result.is_error else "done"
        output = result.output
        artifacts = list(result.artifacts or [])
        error = output if result.is_error else None

        verdict = None
        if status == "done" and self.product_check:
            verdict = self.product_check(
                self._collect_product_text(cwd, output, artifacts))

        self.task_store.update(task_id, status=status, output=output,
                               artifacts=artifacts, error=error, verdict=verdict)

        if self.experience_store:
            try:
                self.experience_store.record_from_task(
                    task_id=task_id, role=role, prompt=prompt, output=output,
                    success=(status == "done"), verdict=verdict,
                    artifacts=artifacts, created_at=ts,
                )
            except Exception:  # noqa: BLE001 - 经验记录失败不拖垮派发
                pass
        write_result_note(self.memory_store, task_id, status, output, error)

        return DispatchResult(task_id=task_id, status=status, output=output,
                              artifacts=artifacts, error=error, verdict=verdict)

    def redispatch(self, task_id: str, model: str | None = None) -> DispatchResult:
        """按已存在任务的 role/prompt 重新本地执行，返回新 DispatchResult。"""
        rec = self.task_store.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        return self.dispatch(role=rec.role, prompt=rec.prompt,
                             context_memory=None, model=model)
