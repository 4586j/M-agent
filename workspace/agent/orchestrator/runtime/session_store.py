"""会话存储（brain 派发链路连续对话）：把派发的轮次落盘，续聊时注入历史。

与 agent_core.service 的持久会话（panel 单机链路）同构、各自独立：
- 每会话一个 JSON 文件（<sessions_dir>/<sid>.json），turns 只记
  {at, prompt, task_id}；任务输出不复制——读取时按 task_id 从 TaskStore
  联表（单一事实源，终态更新零钩子）。
- /dispatch 与 /dispatch/async 带可选 session_id：前置关卡把历史轮次收敛为
  「会话历史」文本块并入 context_memory（复用既有限幅通道），并把本轮
  prompt + task_id 追加进会话文件。

sid 统一服务端生成（12 位 hex），杜绝客户端注入路径。
"""
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

_SID_RE = re.compile(r"[0-9a-f]{12}")
# 注入上下文预算：最近 6 轮、每轮输出截 1200 字符、总块 5000 字符
# （context_memory 另有 clamp_context 总限幅，这里是会话块自身的预算）
_HISTORY_TURNS = 6
_HISTORY_TURN_OUTPUT_CHARS = 1200
_HISTORY_CHARS = 5000


class SessionStore:
    """线程安全的会话轮次账本（JSON 持久化，原子写）。"""

    def __init__(self, sessions_dir: str):
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---- 路径与校验 ----

    def _path(self, session_id: str) -> Path:
        if not _SID_RE.fullmatch(session_id):
            raise KeyError(f"session_id 非法（须为 12 位 hex）: {session_id!r}")
        return self._dir / f"{session_id}.json"

    # ---- 基础读写 ----

    def create(self) -> dict:
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        rec = {"session_id": sid, "title": "", "created_at": now,
               "updated_at": now, "turns": []}
        self._write(self._path(sid), rec)
        return rec

    def get(self, session_id: str) -> dict | None:
        try:
            p = self._path(session_id)
        except KeyError:
            return None
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            return rec if isinstance(rec, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            log.warning("会话文件 %s 解析失败: %s", p, e)
            return None

    def list_all(self) -> list[dict]:
        out = []
        for p in sorted(self._dir.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 损坏文件跳过
                continue
            if isinstance(rec, dict):
                out.append({
                    "session_id": rec.get("session_id") or p.stem,
                    "title": rec.get("title") or "",
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                    "turn_count": len(rec.get("turns") or []),
                })
        return out

    def delete(self, session_id: str) -> bool:
        try:
            p = self._path(session_id)
        except KeyError:
            return False
        if not p.exists():
            return False
        p.unlink()
        return True

    # ---- 轮次 ----

    def append_turn(self, session_id: str, prompt: str, task_id: str) -> None:
        """追加一轮（prompt + task_id；输出读取时从 TaskStore 联表）。"""
        rec = self.get(session_id)
        if rec is None:
            raise KeyError(f"session not found: {session_id}")
        with self._lock:
            rec.setdefault("turns", []).append({
                "at": round(time.time(), 3),
                "prompt": prompt,
                "task_id": task_id,
            })
            rec["updated_at"] = time.time()
            if not rec.get("title"):
                rec["title"] = prompt[:40]
            self._write(self._path(session_id), rec)

    def history_block(self, session_id: str, task_lookup) -> str:
        """把会话历史收敛为注入 context 的文本块。

        task_lookup(task_id) -> TaskRecord | None：联表取任务输出/状态，
        终态任务的 output 截断后作为「助手回复」参与注入。
        """
        rec = self.get(session_id)
        if rec is None:
            return ""
        lines: list[str] = []
        for t in (rec.get("turns") or [])[-_HISTORY_TURNS:]:
            task = None
            try:
                task = task_lookup(t.get("task_id") or "")
            except Exception:  # noqa: BLE001 - 联表失败降级为无输出
                task = None
            output = "（执行中或无输出）"
            if task is not None and getattr(task, "status", "") in ("done", "failed"):
                output = str(getattr(task, "output") or "（无输出）")
                if len(output) > _HISTORY_TURN_OUTPUT_CHARS:
                    output = output[:_HISTORY_TURN_OUTPUT_CHARS] + "…"
            lines.append(f"用户：{t.get('prompt', '')}\n助手：{output}")
        if not lines:
            return ""
        block = "\n\n".join(lines)
        if len(block) > _HISTORY_CHARS:
            block = block[-_HISTORY_CHARS:]
        return "【会话历史（此前轮次摘要，供延续对话用）】\n" + block

    # ---- 持久化 ----

    @staticmethod
    def _write(path: Path, rec: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
        except OSError as e:
            log.warning("会话文件写入失败 %s: %s", path, e)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
