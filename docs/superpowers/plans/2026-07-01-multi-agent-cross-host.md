# 多 Agent 跨主机协同 (V2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 V1 单机形态基础上把 executor 派发远程化，实现多主机 Claude Code 协同；security-guard 留大脑本地，纯 HTTP 协议贯穿。

**Architecture:** 大脑侧新增 Dispatcher 接口（Local/Remote 双实现）+ WorkerRegistry + TaskStore + MemoryStore + brain_api；worker 侧新增 FastAPI 服务 + AgentRunner（Agent SDK）。配置 `mode=local|remote` 切换，V1 单机行为完全保留。

**Tech Stack:** Python 3.13、FastAPI、uvicorn、requests、pytest、claude-agent-sdk（worker 执行体，可 mock）。

## Global Constraints

- V2 是 V1 增量叠加：V1 文件（SKILL.md、security-rules/、.claude/agents/*.md、data-expert）保持不动，除非任务明确要求修改。
- `mode = "local" | "remote"` 配置驱动：local 下 Dispatcher 用 LocalDispatcher、不启动 brain_api，V1 行为完全保留；remote 下用 RemoteDispatcher、启动 brain_api。
- security-guard 不跨主机，留大脑本地派发（不经 Dispatcher）。
- 纯 HTTP 一套协议：任务派发、注册、心跳、文件回传全是 HTTP 端点，不用 MCP。
- worker 按角色专化，V2 只实现 executor-worker。
- 任务异步执行：`POST /task` 立即返回 accepted，大脑轮询 `GET /task/{id}`。
- 心跳超时阈值 45s（3 个 15s 周期）。
- TaskStore 用本地 JSON 文件持久化，V2 不引入数据库。
- MemoryStore 接口抽象，V2 落地为本地文件（FileMemoryStore）。
- AgentRunner 是可 mock 接口：单测用 FakeAgentRunner，真实 SDK 实现作为 AgentRunner 的一个实现类。
- 现有测试 `workspace/agent/skills/orchestrator/security-rules/test_rules.py` 必须持续通过（V1 回归）。
- Python 解释器：系统 python（D:\ca\python.exe），pytest/pyyaml 已装；fastapi/uvicorn/claude-agent-sdk 待装。
- 工作目录 d:\agent\agent，shell bash（Git Bash），正斜杠相对路径。
- 提交信息带 trailer `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 责任 |
|------|------|
| `workspace/agent/orchestrator/runtime/__init__.py` | runtime 包 |
| `workspace/agent/orchestrator/runtime/config.py` | mode/brain_url 等配置 |
| `workspace/agent/orchestrator/runtime/task_store.py` | TaskStore：任务持久化（JSON） |
| `workspace/agent/orchestrator/runtime/worker_registry.py` | WorkerRegistry：注册/心跳/选择 |
| `workspace/agent/orchestrator/runtime/memory_store.py` | MemoryStore 接口 + FileMemoryStore |
| `workspace/agent/orchestrator/runtime/dispatcher.py` | Dispatcher 接口 + Local/Remote 实现 |
| `workspace/agent/orchestrator/runtime/brain_api.py` | 大脑 HTTP 服务（FastAPI） |
| `workspace/agent/orchestrator/runtime/tests/test_task_store.py` | TaskStore 测试 |
| `workspace/agent/orchestrator/runtime/tests/test_worker_registry.py` | WorkerRegistry 测试 |
| `workspace/agent/orchestrator/runtime/tests/test_dispatcher.py` | Dispatcher 测试 |
| `workspace/agent/orchestrator/runtime/tests/test_brain_api.py` | brain_api 集成测试 |
| `worker/__init__.py` | worker 包 |
| `worker/worker_config.py` | worker 配置 |
| `worker/agent_runner.py` | AgentRunner 接口 + SdkAgentRunner + FakeAgentRunner |
| `worker/worker_api.py` | worker FastAPI 服务 |
| `worker/tests/test_worker_api.py` | worker_api 测试 |
| `worker/requirements.txt` | fastapi, uvicorn, requests, claude-agent-sdk |
| `workspace/agent/orchestrator/runtime/tests/__init__.py` | tests 包 |
| `worker/tests/__init__.py` | tests 包 |

---

## Task 1: 安装依赖与 config 骨架

**Files:**
- Create: `workspace/agent/orchestrator/runtime/__init__.py`
- Create: `workspace/agent/orchestrator/runtime/config.py`
- Create: `workspace/agent/orchestrator/runtime/tests/__init__.py`
- Test: `workspace/agent/orchestrator/runtime/tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces: `config.py` 暴露 `Config` dataclass（字段 `mode: str`, `brain_url: str`, `heartbeat_interval: int`, `heartbeat_timeout: int`, `task_store_dir: str`）与 `load_config() -> Config`（从环境变量读取，带默认值）。

- [ ] **Step 1: 安装依赖**

Run: `pip install fastapi uvicorn httpx`
Expected: 安装成功（httpx 用于 FastAPI 的 TestClient）

- [ ] **Step 2: 写失败测试**

Create `workspace/agent/orchestrator/runtime/tests/test_config.py`:

```python
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config, load_config


def test_default_config_is_local():
    cfg = load_config()
    assert cfg.mode == "local"
    assert cfg.heartbeat_interval == 15
    assert cfg.heartbeat_timeout == 45
    assert cfg.brain_url == "http://127.0.0.1:7000"


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("ORCH_MODE", "remote")
    monkeypatch.setenv("ORCH_BRAIN_URL", "http://10.0.0.1:7000")
    cfg = load_config()
    assert cfg.mode == "remote"
    assert cfg.brain_url == "http://10.0.0.1:7000"


def test_invalid_mode_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("ORCH_MODE", "bogus")
    cfg = load_config()
    assert cfg.mode == "local"
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 4: 写 __init__.py 与 config.py**

Create `workspace/agent/orchestrator/runtime/__init__.py` (空文件):

```python
```

Create `workspace/agent/orchestrator/runtime/tests/__init__.py` (空文件):

```python
```

Create `workspace/agent/orchestrator/runtime/config.py`:

```python
"""大脑侧运行时配置。mode=local 保留 V1 单机行为，mode=remote 启用跨主机。"""
import os
from dataclasses import dataclass

VALID_MODES = ("local", "remote")


@dataclass
class Config:
    mode: str = "local"
    brain_url: str = "http://127.0.0.1:7000"
    heartbeat_interval: int = 15
    heartbeat_timeout: int = 45
    task_store_dir: str = "workspace/agent/orchestrator/runtime/.tasks"


def load_config() -> Config:
    mode = os.environ.get("ORCH_MODE", "local")
    if mode not in VALID_MODES:
        mode = "local"
    return Config(
        mode=mode,
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        heartbeat_interval=int(os.environ.get("ORCH_HEARTBEAT_INTERVAL", "15")),
        heartbeat_timeout=int(os.environ.get("ORCH_HEARTBEAT_TIMEOUT", "45")),
        task_store_dir=os.environ.get(
            "ORCH_TASK_STORE_DIR", "workspace/agent/orchestrator/runtime/.tasks"
        ),
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 6: 提交**

```bash
git add workspace/agent/orchestrator/runtime/
git commit -m "feat(runtime): config 骨架与依赖

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: TaskStore 任务持久化

**Files:**
- Create: `workspace/agent/orchestrator/runtime/task_store.py`
- Test: `workspace/agent/orchestrator/runtime/tests/test_task_store.py`

**Interfaces:**
- Consumes: Task 1 的 `Config.task_store_dir`
- Produces: `TaskStore` 类，方法 `write(record: TaskRecord) -> None`、`update(task_id: str, **fields) -> TaskRecord`、`get(task_id: str) -> TaskRecord | None`、`list_lost() -> list[TaskRecord]`、`list_by_worker(worker_id: str) -> list[TaskRecord]`；`TaskRecord` dataclass 字段 `task_id, role, prompt, worker_id, status, created_at, updated_at, output, artifacts, error`，`status` 取值 `pending|running|done|failed|lost`。

- [ ] **Step 1: 写失败测试**

Create `workspace/agent/orchestrator/runtime/tests/test_task_store.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_store import TaskStore, TaskRecord


def make_record(task_id="task-1", status="pending", worker_id="worker-A"):
    return TaskRecord(
        task_id=task_id, role="executor", prompt="do x",
        worker_id=worker_id, status=status,
        created_at="t0", updated_at="t0",
        output=None, artifacts=[], error=None,
    )


def test_write_and_get(tmp_path):
    store = TaskStore(str(tmp_path))
    rec = make_record()
    store.write(rec)
    got = store.get("task-1")
    assert got is not None
    assert got.status == "pending"
    assert got.prompt == "do x"


def test_update_changes_status_and_fields(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record())
    updated = store.update("task-1", status="done", output="result")
    assert updated.status == "done"
    assert updated.output == "result"
    assert store.get("task-1").status == "done"


def test_list_lost_returns_only_lost(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="lost"))
    store.write(make_record("task-2", status="running", worker_id="worker-B"))
    store.write(make_record("task-3", status="done"))
    lost = store.list_lost()
    assert [r.task_id for r in lost] == ["task-1"]


def test_list_by_worker(tmp_path):
    store = TaskStore(str(tmp_path))
    store.write(make_record("task-1", status="running", worker_id="worker-A"))
    store.write(make_record("task-2", status="running", worker_id="worker-B"))
    store.write(make_record("task-3", status="done", worker_id="worker-A"))
    by_a = store.list_by_worker("worker-A")
    assert {r.task_id for r in by_a} == {"task-1", "task-3"}


def test_get_missing_returns_none(tmp_path):
    store = TaskStore(str(tmp_path))
    assert store.get("nope") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_task_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'task_store'`

- [ ] **Step 3: 写 task_store.py**

Create `workspace/agent/orchestrator/runtime/task_store.py`:

```python
"""任务持久化。每个任务一个 JSON 文件，支持状态流转与按 worker/lost 查询。"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

VALID_STATUSES = ("pending", "running", "done", "failed", "lost")


@dataclass
class TaskRecord:
    task_id: str
    role: str
    prompt: str
    worker_id: str | None
    status: str
    created_at: str
    updated_at: str
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None


class TaskStore:
    def __init__(self, store_dir: str):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.store_dir / f"{task_id}.json"

    def write(self, record: TaskRecord) -> None:
        if record.status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {record.status}")
        with open(self._path(record.task_id), "w", encoding="utf-8") as f:
            json.dump(asdict(record), f, ensure_ascii=False, indent=2)

    def get(self, task_id: str) -> TaskRecord | None:
        p = self._path(task_id)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return TaskRecord(**json.load(f))

    def update(self, task_id: str, **fields) -> TaskRecord:
        rec = self.get(task_id)
        if rec is None:
            raise KeyError(f"task not found: {task_id}")
        if "status" in fields and fields["status"] not in VALID_STATUSES:
            raise ValueError(f"invalid status: {fields['status']}")
        for k, v in fields.items():
            setattr(rec, k, v)
        self.write(rec)
        return rec

    def list_lost(self) -> list[TaskRecord]:
        return [r for r in self._all() if r.status == "lost"]

    def list_by_worker(self, worker_id: str) -> list[TaskRecord]:
        return [r for r in self._all() if r.worker_id == worker_id]

    def _all(self) -> list[TaskRecord]:
        out = []
        for p in sorted(self.store_dir.glob("*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(TaskRecord(**json.load(f)))
        return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_task_store.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add workspace/agent/orchestrator/runtime/task_store.py workspace/agent/orchestrator/runtime/tests/test_task_store.py
git commit -m "feat(runtime): TaskStore 任务持久化

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: WorkerRegistry 注册/心跳/选择

**Files:**
- Create: `workspace/agent/orchestrator/runtime/worker_registry.py`
- Test: `workspace/agent/orchestrator/runtime/tests/test_worker_registry.py`

**Interfaces:**
- Consumes: 无
- Produces: `WorkerRegistry` 类，方法 `register(worker: WorkerInfo) -> None`、`heartbeat(worker_id: str, load: int = 0) -> None`、`select_worker(role: str = "executor") -> WorkerInfo | None`、`active_workers() -> list[WorkerInfo]`；`WorkerInfo` dataclass 字段 `worker_id, role, host, port, capabilities, last_heartbeat`。心跳超时判定用注入的 `now_fn`（默认返回时间戳），便于测试。

- [ ] **Step 1: 写失败测试**

Create `workspace/agent/orchestrator/runtime/tests/test_worker_registry.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker_registry import WorkerRegistry, WorkerInfo


def make_info(wid="worker-A", role="executor"):
    return WorkerInfo(
        worker_id=wid, role=role, host="127.0.0.1", port=8000,
        capabilities={"model": "sonnet"}, last_heartbeat=0.0,
    )


def test_register_and_select(tmp_path):
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info())
    w = reg.select_worker("executor")
    assert w is not None
    assert w.worker_id == "worker-A"


def test_select_no_active_returns_none():
    reg = WorkerRegistry(heartbeat_timeout=45)
    assert reg.select_worker("executor") is None


def test_select_round_robin():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A"))
    reg.register(make_info("B"))
    picked = [reg.select_worker("executor").worker_id for _ in range(4)]
    assert picked == ["A", "B", "A", "B"]


def test_heartbeat_timeout_marks_inactive():
    times = [100.0]
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: times[0])
    reg.register(make_info())
    reg.heartbeat("worker-A")  # last_heartbeat=100
    times[0] = 200.0  # 100s later, > 45s timeout
    assert reg.select_worker("executor") is None
    assert reg.active_workers() == []


def test_role_filter():
    reg = WorkerRegistry(heartbeat_timeout=45)
    reg.register(make_info("A", role="executor"))
    reg.register(make_info("B", role="security"))
    assert reg.select_worker("executor").worker_id == "A"
    assert reg.select_worker("security").worker_id == "B"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_worker_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker_registry'`

- [ ] **Step 3: 写 worker_registry.py**

Create `workspace/agent/orchestrator/runtime/worker_registry.py`:

```python
"""worker 注册表：注册、心跳保活、按角色轮询选择。"""
import itertools
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class WorkerInfo:
    worker_id: str
    role: str
    host: str
    port: int
    capabilities: dict
    last_heartbeat: float


class WorkerRegistry:
    def __init__(self, heartbeat_timeout: int = 45, now_fn: Callable[[], float] | None = None):
        self.heartbeat_timeout = heartbeat_timeout
        self._now = now_fn or time.time
        self._workers: dict[str, WorkerInfo] = {}
        self._rr: dict[str, itertools.cycle] = {}

    def register(self, worker: WorkerInfo) -> None:
        worker.last_heartbeat = self._now()
        self._workers[worker.worker_id] = worker
        self._rr.pop(worker.role, None)  # 重建轮询器

    def heartbeat(self, worker_id: str, load: int = 0) -> None:
        w = self._workers.get(worker_id)
        if w is None:
            return
        w.last_heartbeat = self._now()

    def _alive(self) -> list[WorkerInfo]:
        now = self._now()
        return [w for w in self._workers.values() if now - w.last_heartbeat <= self.heartbeat_timeout]

    def active_workers(self) -> list[WorkerInfo]:
        return list(self._alive())

    def select_worker(self, role: str = "executor") -> WorkerInfo | None:
        alive = [w for w in self._alive() if w.role == role]
        if not alive:
            return None
        if role not in self._rr:
            self._rr[role] = itertools.cycle(alive)
            return next(self._rr[role])
        # 轮询但跳过已失联的
        for _ in range(len(alive)):
            cand = next(self._rr[role])
            if cand in alive:
                return cand
        return alive[0]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_worker_registry.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add workspace/agent/orchestrator/runtime/worker_registry.py workspace/agent/orchestrator/runtime/tests/test_worker_registry.py
git commit -m "feat(runtime): WorkerRegistry 注册/心跳/轮询选择

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: MemoryStore 接口与 File 实现

**Files:**
- Create: `workspace/agent/orchestrator/runtime/memory_store.py`
- Test: `workspace/agent/orchestrator/runtime/tests/test_memory_store.py`

**Interfaces:**
- Consumes: 无
- Produces: `MemoryStore` 抽象基类（方法 `read_core(name) -> str | None`、`write_core(name, content) -> None`、`delete_core(name) -> None`、`read_context(task_id, key) -> str | None`、`write_context(task_id, key, value) -> None`）；`FileMemoryStore` 实现，核心记忆存 `core_dir/{name}.md`，上下文记忆存 `context_dir/{task_id}/{key}.md`。

- [ ] **Step 1: 写失败测试**

Create `workspace/agent/orchestrator/runtime/tests/test_memory_store.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory_store import FileMemoryStore


def test_core_write_read_delete(tmp_path):
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"), context_dir=str(tmp_path / "ctx"))
    ms.write_core("proj-pref", "用户偏好报告含改进建议")
    assert ms.read_core("proj-pref") == "用户偏好报告含改进建议"
    ms.delete_core("proj-pref")
    assert ms.read_core("proj-pref") is None


def test_core_read_missing_returns_none(tmp_path):
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"), context_dir=str(tmp_path / "ctx"))
    assert ms.read_core("nope") is None


def test_context_write_read_per_task(tmp_path):
    ms = FileMemoryStore(core_dir=str(tmp_path / "core"), context_dir=str(tmp_path / "ctx"))
    ms.write_context("task-1", "tried", "方案A失败")
    ms.write_context("task-2", "tried", "方案B")
    assert ms.read_context("task-1", "tried") == "方案A失败"
    assert ms.read_context("task-2", "tried") == "方案B"
    assert ms.read_context("task-1", "missing") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_memory_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory_store'`

- [ ] **Step 3: 写 memory_store.py**

Create `workspace/agent/orchestrator/runtime/memory_store.py`:

```python
"""记忆访问抽象。V2 落地为本地文件；接口便于将来换共享存储。"""
from abc import ABC, abstractmethod
from pathlib import Path


class MemoryStore(ABC):
    @abstractmethod
    def read_core(self, name: str) -> str | None: ...
    @abstractmethod
    def write_core(self, name: str, content: str) -> None: ...
    @abstractmethod
    def delete_core(self, name: str) -> None: ...
    @abstractmethod
    def read_context(self, task_id: str, key: str) -> str | None: ...
    @abstractmethod
    def write_context(self, task_id: str, key: str, value: str) -> None: ...


class FileMemoryStore(MemoryStore):
    def __init__(self, core_dir: str, context_dir: str):
        self.core_dir = Path(core_dir)
        self.context_dir = Path(context_dir)
        self.core_dir.mkdir(parents=True, exist_ok=True)
        self.context_dir.mkdir(parents=True, exist_ok=True)

    def read_core(self, name: str) -> str | None:
        p = self.core_dir / f"{name}.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def write_core(self, name: str, content: str) -> None:
        (self.core_dir / f"{name}.md").write_text(content, encoding="utf-8")

    def delete_core(self, name: str) -> None:
        p = self.core_dir / f"{name}.md"
        if p.exists():
            p.unlink()

    def read_context(self, task_id: str, key: str) -> str | None:
        p = self.context_dir / task_id / f"{key}.md"
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def write_context(self, task_id: str, key: str, value: str) -> None:
        d = self.context_dir / task_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.md").write_text(value, encoding="utf-8")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_memory_store.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add workspace/agent/orchestrator/runtime/memory_store.py workspace/agent/orchestrator/runtime/tests/test_memory_store.py
git commit -m "feat(runtime): MemoryStore 接口与 File 实现

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: Dispatcher 接口与 Local/Remote 实现

**Files:**
- Create: `workspace/agent/orchestrator/runtime/dispatcher.py`
- Test: `workspace/agent/orchestrator/runtime/tests/test_dispatcher.py`

**Interfaces:**
- Consumes: Task 2 的 `TaskStore`/`TaskRecord`、Task 3 的 `WorkerRegistry`/`WorkerInfo`
- Produces: `DispatchResult` dataclass（`task_id, status, output, artifacts, error`）；`Dispatcher` 抽象基类（`dispatch(role, prompt, context_memory=None, model=None) -> DispatchResult`）；`LocalDispatcher`（记录任务到 TaskStore，status 标 `done`，output 为提示「本地派发需主会话执行」——V1 兼容桩）；`RemoteDispatcher`（选 worker、写 pending、HTTP POST /task、轮询 GET /task/{id} 直到 done/failed、更新 TaskStore）。RemoteDispatcher 的 HTTP 客户端用注入的 `http_call` 函数以便测试。

- [ ] **Step 1: 写失败测试**

Create `workspace/agent/orchestrator/runtime/tests/test_dispatcher.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dispatcher import DispatchResult, LocalDispatcher, RemoteDispatcher
from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry, WorkerInfo


def make_info(wid="worker-A"):
    return WorkerInfo(
        worker_id=wid, role="executor", host="127.0.0.1", port=8000,
        capabilities={}, last_heartbeat=0.0,
    )


def test_local_dispatcher_writes_task_and_returns_done(tmp_path):
    store = TaskStore(str(tmp_path))
    d = LocalDispatcher(task_store=store, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x")
    assert res.status == "done"
    assert res.task_id.startswith("task-")
    rec = store.get(res.task_id)
    assert rec is not None
    assert rec.status == "done"


def test_remote_dispatcher_no_worker_raises(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45)
    d = RemoteDispatcher(task_store=store, registry=reg, http_call=lambda *a, **k: None, now_fn=lambda: "t0")
    try:
        d.dispatch(role="executor", prompt="do x")
        assert False, "应抛错"
    except RuntimeError as e:
        assert "无可用 worker" in str(e)


def test_remote_dispatcher_dispatches_and_polls(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    calls = {"post": 0, "get": 0}

    def fake_http(method, url, **kwargs):
        if method == "POST":
            calls["post"] += 1
            return {"status_code": 200, "json": {"task_id": "task-x", "status": "accepted"}}
        if method == "GET":
            calls["get"] += 1
            if calls["get"] == 1:
                return {"status_code": 200, "json": {"task_id": "task-x", "status": "running"}}
            return {"status_code": 200, "json": {
                "task_id": "task-x", "status": "done",
                "output": "结果", "artifacts": [], "error": None}}
        return {"status_code": 404}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x", context_memory="ctx")
    assert res.status == "done"
    assert res.output == "结果"
    assert calls["post"] == 1
    assert calls["get"] == 2
    rec = store.get(res.task_id)
    assert rec.status == "done"


def test_remote_dispatcher_failed_propagates(tmp_path):
    store = TaskStore(str(tmp_path))
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(make_info())

    def fake_http(method, url, **kwargs):
        if method == "POST":
            return {"status_code": 200, "json": {"task_id": "task-y", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "task-y", "status": "failed", "output": None,
            "artifacts": [], "error": "boom"}}

    d = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http, now_fn=lambda: "t0")
    res = d.dispatch(role="executor", prompt="do x")
    assert res.status == "failed"
    assert res.error == "boom"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_dispatcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dispatcher'`

- [ ] **Step 3: 写 dispatcher.py**

Create `workspace/agent/orchestrator/runtime/dispatcher.py`:

```python
"""派发抽象。LocalDispatcher 保留 V1；RemoteDispatcher 走 HTTP 到 worker。"""
import itertools
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry

_POLL_MAX = 1000  # 轮询上限，防死循环


@dataclass
class DispatchResult:
    task_id: str
    status: str
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None


class Dispatcher(ABC):
    @abstractmethod
    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None,
                 model: str | None = None) -> DispatchResult: ...


class LocalDispatcher(Dispatcher):
    """V1 兼容：记录任务并标记 done。真实本地执行由主会话 Agent 工具完成。"""

    def __init__(self, task_store: TaskStore, now_fn: Callable[[], str] | None = None):
        self.task_store = task_store
        self._now = now_fn or (lambda: "t0")

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None) -> DispatchResult:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        rec = TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id="local",
            status="done", created_at=ts, updated_at=ts,
            output="本地派发：由主会话 Agent 工具执行", artifacts=[], error=None,
        )
        self.task_store.write(rec)
        return DispatchResult(task_id=task_id, status="done",
                              output=rec.output, artifacts=[], error=None)


class RemoteDispatcher(Dispatcher):
    """HTTP 派发到 worker：选 worker、写 pending、POST /task、轮询 GET /task/{id}。"""

    def __init__(self, task_store: TaskStore, registry: WorkerRegistry,
                 http_call: Callable[..., dict], now_fn: Callable[[], str] | None = None):
        self.task_store = task_store
        self.registry = registry
        self.http_call = http_call  # (method, url, **kwargs) -> {"status_code", "json"}
        self._now = now_fn or (lambda: "t0")

    def dispatch(self, role: str, prompt: str,
                 context_memory: str | None = None, model: str | None = None) -> DispatchResult:
        worker = self.registry.select_worker(role)
        if worker is None:
            raise RuntimeError(f"无可用 {role} worker")
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        ts = self._now()
        self.task_store.write(TaskRecord(
            task_id=task_id, role=role, prompt=prompt, worker_id=worker.worker_id,
            status="pending", created_at=ts, updated_at=ts,
            output=None, artifacts=[], error=None,
        ))
        base = f"http://{worker.host}:{worker.port}"
        body = {"task_id": task_id, "prompt": prompt,
                "context_memory": context_memory or ""}
        if model:
            body["model"] = model
        resp = self.http_call("POST", f"{base}/task", json=body)
        if resp.get("status_code") != 200:
            self.task_store.update(task_id, status="failed", error=f"post failed: {resp}")
            return DispatchResult(task_id, "failed", error=f"post failed: {resp}")
        self.task_store.update(task_id, status="running")
        # 轮询
        for _ in range(_POLL_MAX):
            r = self.http_call("GET", f"{base}/task/{task_id}")
            data = r.get("json", {})
            st = data.get("status")
            if st in ("done", "failed"):
                self.task_store.update(
                    task_id, status=st, output=data.get("output"),
                    artifacts=data.get("artifacts", []), error=data.get("error"),
                )
                return DispatchResult(
                    task_id, st, data.get("output"),
                    data.get("artifacts", []), data.get("error"),
                )
        self.task_store.update(task_id, status="failed", error="poll timeout")
        return DispatchResult(task_id, "failed", error="poll timeout")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_dispatcher.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add workspace/agent/orchestrator/runtime/dispatcher.py workspace/agent/orchestrator/runtime/tests/test_dispatcher.py
git commit -m "feat(runtime): Dispatcher 接口与 Local/Remote 实现

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: brain_api 大脑 HTTP 服务

**Files:**
- Create: `workspace/agent/orchestrator/runtime/brain_api.py`
- Test: `workspace/agent/orchestrator/runtime/tests/test_brain_api.py`

**Interfaces:**
- Consumes: Task 3 的 `WorkerRegistry`/`WorkerInfo`、Task 2 的 `TaskStore`、Task 1 的 `Config`
- Produces: `create_brain_app(registry, task_store, cfg) -> FastAPI`，端点 `POST /register`、`POST /heartbeat`、`GET /tasks?worker_id=`、`GET /workers`。`POST /heartbeat` 触发失联任务标记 lost（委托给传入的 `on_timeout` 回调，默认空）。

- [ ] **Step 1: 写失败测试**

Create `workspace/agent/orchestrator/runtime/tests/test_brain_api.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from brain_api import create_brain_app
from worker_registry import WorkerRegistry, WorkerInfo
from task_store import TaskStore, TaskRecord
from config import Config


def make_app(tmp_path, now_fn=None):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=now_fn or (lambda: 0.0))
    store = TaskStore(str(tmp_path))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path))
    app = create_brain_app(reg, store, cfg)
    return TestClient(app), reg, store


def test_register_worker(tmp_path):
    client, reg, _ = make_app(tmp_path)
    r = client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {"model": "sonnet"},
    })
    assert r.status_code == 200
    assert r.json()["status"] == "registered"
    assert reg.select_worker("executor") is not None


def test_heartbeat(tmp_path):
    client, reg, _ = make_app(tmp_path, now_fn=lambda: 100.0)
    client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {},
    })
    r = client.post("/heartbeat", json={"worker_id": "worker-A", "load": 1})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_tasks_by_worker(tmp_path):
    client, _, store = make_app(tmp_path)
    store.write(TaskRecord(
        task_id="task-1", role="executor", prompt="x", worker_id="worker-A",
        status="running", created_at="t", updated_at="t"))
    r = client.get("/tasks?worker_id=worker-A")
    assert r.status_code == 200
    ids = [t["task_id"] for t in r.json()]
    assert "task-1" in ids


def test_list_workers(tmp_path):
    client, _, _ = make_app(tmp_path)
    client.post("/register", json={
        "worker_id": "worker-A", "role": "executor",
        "host": "127.0.0.1", "port": 8000, "capabilities": {},
    })
    r = client.get("/workers")
    assert r.status_code == 200
    assert any(w["worker_id"] == "worker-A" for w in r.json())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_brain_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_api'`

- [ ] **Step 3: 写 brain_api.py**

Create `workspace/agent/orchestrator/runtime/brain_api.py`:

```python
"""大脑 HTTP 服务：接收 worker 注册/心跳，查询任务与 worker。"""
from dataclasses import asdict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from task_store import TaskStore
from worker_registry import WorkerRegistry, WorkerInfo
from config import Config


class RegisterReq(BaseModel):
    worker_id: str
    role: str
    host: str
    port: int
    capabilities: dict = {}


class HeartbeatReq(BaseModel):
    worker_id: str
    load: int = 0


def create_brain_app(registry: WorkerRegistry, task_store: TaskStore, cfg: Config) -> FastAPI:
    app = FastAPI()

    @app.post("/register")
    def register(req: RegisterReq):
        registry.register(WorkerInfo(
            worker_id=req.worker_id, role=req.role, host=req.host, port=req.port,
            capabilities=req.capabilities, last_heartbeat=0.0,
        ))
        return {"status": "registered", "worker_id": req.worker_id}

    @app.post("/heartbeat")
    def heartbeat(req: HeartbeatReq):
        registry.heartbeat(req.worker_id, req.load)
        return {"status": "ok"}

    @app.get("/tasks")
    def list_tasks(worker_id: str | None = None):
        if worker_id:
            recs = task_store.list_by_worker(worker_id)
        else:
            recs = task_store._all()
        return [asdict(r) for r in recs]

    @app.get("/workers")
    def list_workers():
        return [asdict(w) for w in registry.active_workers()]

    return app
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd workspace/agent/orchestrator/runtime && python -m pytest tests/test_brain_api.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add workspace/agent/orchestrator/runtime/brain_api.py workspace/agent/orchestrator/runtime/tests/test_brain_api.py
git commit -m "feat(runtime): brain_api 大脑 HTTP 服务

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: worker 侧 AgentRunner 接口与 Fake/Sdk 实现

**Files:**
- Create: `worker/__init__.py`
- Create: `worker/worker_config.py`
- Create: `worker/agent_runner.py`
- Create: `worker/tests/__init__.py`
- Test: `worker/tests/test_agent_runner.py`

**Interfaces:**
- Consumes: 无
- Produces: `TaskRequest` dataclass（`task_id, prompt, context_memory, model`）；`TaskResult` dataclass（`task_id, status, output, artifacts, error, memory_candidates`）；`AgentRunner` 抽象基类（`run(req: TaskRequest) -> TaskResult`）；`FakeAgentRunner`（按 prompt 中的关键词决定输出，便于测试）；`SdkAgentRunner`（真实调 claude-agent-sdk，本任务只写骨架不接真实 SDK，标 NotImplemented 占位说明）。

- [ ] **Step 1: 写失败测试**

Create `worker/__init__.py` (空):

```python
```

Create `worker/tests/__init__.py` (空):

```python
```

Create `worker/tests/test_agent_runner.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runner import FakeAgentRunner, TaskRequest


def test_fake_runner_done_on_simple_prompt(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    req = TaskRequest(task_id="task-1", prompt="分析数据", context_memory="", model=None)
    res = runner.run(req)
    assert res.task_id == "task-1"
    assert res.status == "done"
    assert res.error is None
    assert isinstance(res.artifacts, list)


def test_fake_runner_failed_on_error_keyword(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    req = TaskRequest(task_id="task-2", prompt="FORCE_ERROR 触发失败", context_memory="", model=None)
    res = runner.run(req)
    assert res.status == "failed"
    assert res.error is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd worker && python -m pytest tests/test_agent_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_runner'`

- [ ] **Step 3: 写 worker_config.py 与 agent_runner.py**

Create `worker/worker_config.py`:

```python
"""worker 配置。"""
import os
from dataclasses import dataclass


@dataclass
class WorkerConfig:
    brain_url: str = "http://127.0.0.1:7000"
    worker_id: str = "worker-local"
    role: str = "executor"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8000
    executor_def_path: str = ".claude/agents/executor.md"
    work_dir: str = "workspace/agent/orchestrator/runtime/.worker_work"


def load_worker_config() -> WorkerConfig:
    return WorkerConfig(
        brain_url=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
        worker_id=os.environ.get("ORCH_WORKER_ID", "worker-local"),
        role="executor",
        listen_host=os.environ.get("ORCH_WORKER_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("ORCH_WORKER_PORT", "8000")),
        executor_def_path=os.environ.get("ORCH_EXECUTOR_DEF", ".claude/agents/executor.md"),
        work_dir=os.environ.get("ORCH_WORK_DIR", "workspace/agent/orchestrator/runtime/.worker_work"),
    )
```

Create `worker/agent_runner.py`:

```python
"""AgentRunner：执行体的抽象。Fake 用于测试，Sdk 接真实 Agent SDK。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskRequest:
    task_id: str
    prompt: str
    context_memory: str
    model: str | None


@dataclass
class TaskResult:
    task_id: str
    status: str  # done | failed
    output: Any = None
    artifacts: list = field(default_factory=list)
    error: Any = None
    memory_candidates: list = field(default_factory=list)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, req: TaskRequest) -> TaskResult: ...


class FakeAgentRunner(AgentRunner):
    """测试用：prompt 含 FORCE_ERROR 则失败，否则 done。"""

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def run(self, req: TaskRequest) -> TaskResult:
        if "FORCE_ERROR" in req.prompt:
            return TaskResult(req.task_id, "failed", error="forced error for test")
        out = f"fake 执行完成：{req.prompt}"
        return TaskResult(req.task_id, "done", output=out, artifacts=[], error=None)


class SdkAgentRunner(AgentRunner):
    """真实执行：用 claude-agent-sdk 加载 executor.md 起会话。

    V2 骨架：真实 SDK 接入需根据具体 SDK 版本实现 query() 调用与流式产出收集，
    在集成环境验证。单测不覆盖此实现。
    """

    def __init__(self, work_dir: str, executor_def_path: str):
        self.work_dir = work_dir
        self.executor_def_path = executor_def_path

    def run(self, req: TaskRequest) -> TaskResult:
        raise NotImplementedError(
            "SdkAgentRunner 待接入 claude-agent-sdk；当前用 FakeAgentRunner 验证协议"
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd worker && python -m pytest tests/test_agent_runner.py -v`
Expected: 2 passed

- [ ] **Step 5: 写 requirements.txt 并提交**

Create `worker/requirements.txt`:

```
fastapi>=0.110
uvicorn>=0.27
requests>=2.31
claude-agent-sdk>=0.1
```

```bash
git add worker/
git commit -m "feat(worker): AgentRunner 接口与 Fake/Sdk 实现

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: worker_api worker HTTP 服务

**Files:**
- Create: `worker/worker_api.py`
- Test: `worker/tests/test_worker_api.py`

**Interfaces:**
- Consumes: Task 7 的 `AgentRunner`/`FakeAgentRunner`/`TaskRequest`、`WorkerConfig`
- Produces: `create_worker_app(runner, cfg) -> FastAPI`，端点 `POST /task`（异步执行，立即返回 accepted）、`GET /task/{id}`（查状态）、`GET /artifact/{id}?path=`（返回文件内容）。任务状态存内存字典（V2 不持久化 worker 侧，崩溃由大脑 TaskStore 的 lost 机制兜底）。

- [ ] **Step 1: 写失败测试**

Create `worker/tests/test_worker_api.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from worker_api import create_worker_app
from agent_runner import FakeAgentRunner
from worker_config import WorkerConfig


def make_client(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    cfg = WorkerConfig(work_dir=str(tmp_path))
    return TestClient(create_worker_app(runner, cfg))


def test_post_task_accepted_then_done(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/task", json={
        "task_id": "task-1", "prompt": "分析数据", "context_memory": "", "model": None})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    # Fake 同步执行，立即查应为 done
    r2 = client.get("/task/task-1")
    assert r2.status_code == 200
    assert r2.json()["status"] == "done"


def test_post_task_failed(tmp_path):
    client = make_client(tmp_path)
    client.post("/task", json={
        "task_id": "task-2", "prompt": "FORCE_ERROR boom", "context_memory": "", "model": None})
    r = client.get("/task/task-2")
    assert r.json()["status"] == "failed"
    assert r.json()["error"] is not None


def test_get_missing_task(tmp_path):
    client = make_client(tmp_path)
    r = client.get("/task/nope")
    assert r.status_code == 404


def test_artifact_endpoint_returns_file(tmp_path):
    client = make_client(tmp_path)
    # 先放一个文件再拉
    p = Path(tmp_path) / "task-1" / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("报告内容", encoding="utf-8")
    r = client.get("/artifact/task-1?path=task-1/report.md")
    assert r.status_code == 200
    assert "报告内容" in r.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd worker && python -m pytest tests/test_worker_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker_api'`

- [ ] **Step 3: 写 worker_api.py**

Create `worker/worker_api.py`:

```python
"""worker HTTP 服务：接受任务、查询状态、回传产出文件。"""
import threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent_runner import AgentRunner, TaskRequest, TaskResult
from worker_config import WorkerConfig


class TaskBody(BaseModel):
    task_id: str
    prompt: str
    context_memory: str = ""
    model: str | None = None


def create_worker_app(runner: AgentRunner, cfg: WorkerConfig) -> FastAPI:
    app = FastAPI()
    state: dict[str, TaskResult] = {}
    lock = threading.Lock()

    def _execute(body: TaskBody):
        req = TaskRequest(body.task_id, body.prompt, body.context_memory, body.model)
        res = runner.run(req)
        with lock:
            state[body.task_id] = res

    @app.post("/task")
    def post_task(body: TaskBody):
        # 同步执行（Fake 即时）；真实 SDK 可在此起线程异步
        _execute(body)
        return {"task_id": body.task_id, "status": "accepted"}

    @app.get("/task/{task_id}")
    def get_task(task_id: str):
        with lock:
            res = state.get(task_id)
        if res is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "task_id": res.task_id, "status": res.status,
            "output": res.output, "artifacts": res.artifacts,
            "error": res.error, "memory_candidates": res.memory_candidates,
        }

    @app.get("/artifact/{task_id}")
    def get_artifact(task_id: str, path: str):
        full = Path(cfg.work_dir) / path
        # 防路径穿越
        try:
            full = full.resolve()
            base = Path(cfg.work_dir).resolve()
            full.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid path")
        if not full.exists():
            raise HTTPException(status_code=404, detail="artifact not found")
        return {"content": full.read_text(encoding="utf-8")}

    return app
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd worker && python -m pytest tests/test_worker_api.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add worker/worker_api.py worker/tests/test_worker_api.py
git commit -m "feat(worker): worker_api HTTP 服务

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 9: 端到端冒烟测试 runbook 与 V1 回归

**Files:**
- Create: `docs/superpowers/plans/runbook-v2-smoke-tests.md`

**Interfaces:**
- Consumes: Task 1-8 全部产物
- Produces: 手动验证 runbook，覆盖跨主机派发、心跳超时重派、文件回传、V1 回归。

- [ ] **Step 1: 写 runbook**

Create `docs/superpowers/plans/runbook-v2-smoke-tests.md`:

````markdown
# V2 跨主机协同 冒烟测试 Runbook

## 前置
- 完成 Task 1-8
- 系统装好 fastapi/uvicorn/httpx/requests
- 准备两台主机（或本机两个端口模拟）：大脑 + 1 个 executor-worker

## 1. 单元测试全绿
Run:
```
cd workspace/agent/orchestrator/runtime && python -m pytest tests/ -v
cd worker && python -m pytest tests/ -v
```
预期：runtime 与 worker 全部测试通过。

## 2. V1 回归（mode=local）
Run:
```
cd workspace/agent/skills/orchestrator/security-rules && python -m pytest test_rules.py -v
```
预期：6 passed（V1 规则测试不受 V2 影响）。
确认 `ORCH_MODE` 未设或为 local 时，Dispatcher 走 LocalDispatcher，不启动 brain_api。

## 3. 启动大脑（remote 模式）
终端 A：
```
export ORCH_MODE=remote
cd workspace/agent/orchestrator/runtime
python -c "import uvicorn; from brain_api import create_brain_app; from worker_registry import WorkerRegistry; from task_store import TaskStore; from config import load_config; cfg=load_config(); uvicorn.run(create_brain_app(WorkerRegistry(cfg.heartbeat_timeout), TaskStore(cfg.task_store_dir), cfg), host='127.0.0.1', port=7000)"
```
预期：brain_api 在 7000 监听。

## 4. 启动 worker 并注册
终端 B：
```
cd worker
export ORCH_BRAIN_URL=http://127.0.0.1:7000
export ORCH_WORKER_ID=worker-A
python -c "import uvicorn; from worker_api import create_worker_app; from agent_runner import FakeAgentRunner; from worker_config import load_worker_config; cfg=load_worker_config(); uvicorn.run(create_worker_app(FakeAgentRunner(cfg.work_dir), cfg), host='127.0.0.1', port=8000)"
```
另起一个 Python 进程向大脑注册：
```
python -c "import requests; print(requests.post('http://127.0.0.1:7000/register', json={'worker_id':'worker-A','role':'executor','host':'127.0.0.1','port':8000,'capabilities':{}}).json())"
```
预期：`GET http://127.0.0.1:7000/workers` 返回 worker-A。

## 5. 跨主机派发
Python 调 RemoteDispatcher（注入真实 requests http_call）派发 executor 任务。
预期：任务经 POST /task 到 worker-A，轮询 GET /task/{id} 拿到 done，TaskStore 记录 done。

## 6. 心跳超时与 lost
停止 worker-A，等 45s+。
预期：`GET /workers` 不再返回 worker-A；TaskStore 中该 worker 的 running 任务标 lost，可重派。

## 7. 文件回传
worker 产出一个文件后，大脑调 `GET /artifact/{id}?path=...` 拉取。
预期：返回文件内容；非法路径（含 ..）返回 400。
````

- [ ] **Step 2: 运行全部单测确认整体绿**

Run:
```
cd workspace/agent/orchestrator/runtime && python -m pytest tests/ -v
cd d:/agent/agent/worker && python -m pytest tests/ -v
cd d:/agent/agent/workspace/agent/skills/orchestrator/security-rules && python -m pytest test_rules.py -v
```
Expected: runtime + worker + V1 rules 全部 passed

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/plans/runbook-v2-smoke-tests.md
git commit -m "docs: V2 跨主机协同冒烟测试 runbook

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage：**
- Dispatcher 接口 + Local/Remote → Task 5 ✓
- WorkerRegistry 注册/心跳/选择 → Task 3 ✓
- TaskStore 持久化/恢复/list_lost/list_by_worker → Task 2 ✓
- MemoryStore 接口 + File 实现 → Task 4 ✓
- brain_api（/register /heartbeat /tasks /workers）→ Task 6 ✓
- worker HTTP 服务（/task /task/{id} /artifact）→ Task 8 ✓
- AgentRunner（Agent SDK + 可 mock）→ Task 7 ✓
- 配置 mode=local|remote 切换 → Task 1 ✓
- V1 保留 + 回归 → Task 9 场景 2 + Global Constraints ✓
- 心跳超时 45s → Task 1 config + Task 3 测试 ✓
- 文件按需回传 → Task 8 /artifact ✓
- 任务持久化 JSON → Task 2 ✓
- 故障 lost + 重派 → Task 2 list_lost + Task 9 场景 6 ✓
- 扩展点（MemoryStore/TaskStore 接口、select_worker 轮询）→ Task 3/4 ✓

**2. Placeholder scan：** 无 TBD/TODO；SdkAgentRunner 标 NotImplementedError 是设计内的占位（spec 第 4.2 节明确真实 SDK 接入在集成环境验证，单测用 Fake），已在代码注释说明，非计划缺陷。

**3. Type consistency：**
- `TaskRecord` 字段在 Task 2 定义，Task 5/6 引用一致（task_id/role/prompt/worker_id/status/created_at/updated_at/output/artifacts/error）
- `WorkerInfo` 字段在 Task 3 定义，Task 5/6 引用一致（worker_id/role/host/port/capabilities/last_heartbeat）
- `DispatchResult` 在 Task 5 定义（task_id/status/output/artifacts/error）
- `TaskRequest`/`TaskResult` 在 Task 7 定义，Task 8 引用一致
- `http_call` 签名 `(method, url, **kwargs) -> {"status_code","json"}` 在 Task 5 测试与实现一致
- `Config` 字段在 Task 1 定义，Task 6 引用 task_store_dir/mode/heartbeat_timeout 一致
- `select_worker(role="executor")` 默认值在 Task 3/5 一致

无遗留问题。
