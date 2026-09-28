"""运行历史持久化存储。

每次 oneshot 执行完成后，将摘要追加到 JSONL 文件，支持分页查询和趋势统计。
设计目标：
- 文件不存在时自动创建，写入失败静默（不阻断主流程）
- 超过 max_records 时截断旧记录（保留最新的 N 条）
- 线程安全（service 多线程调用 append）
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class RunHistoryStore:
    """JSONL 格式的运行历史存储。

    每条记录一行 JSON，追加写入；查询时全量加载到内存（max_records 上限保护）。
    """

    def __init__(self, history_dir: str, max_records: int = 1000):
        self._dir = Path(history_dir)
        self._max_records = max(1, max_records)  # 至少保留 1 条
        self._lock = threading.Lock()

    @property
    def _path(self) -> Path:
        return self._dir / ".run_history.jsonl"

    def append(self, record: dict) -> None:
        """追加一条运行记录。写入失败静默 log warning。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("run_history: 创建目录失败 %s: %s", self._dir, e)
            return

        # 确保必要字段
        record.setdefault("run_id", str(uuid.uuid4()))
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        try:
            with self._lock:
                # 先读取现有记录
                records = self._load_records()
                records.append(record)
                # 截断旧记录
                if len(records) > self._max_records:
                    records = records[-self._max_records:]
                # 写回文件
                self._write_records(records)
        except Exception as e:
            logger.warning("run_history: 追加记录失败: %s", e)

    def query(self, limit: int = 50, offset: int = 0,
              status: str = "all") -> tuple[list[dict], int]:
        """分页查询历史记录。

        Args:
            limit: 返回条数上限
            offset: 跳过条数
            status: "all" | "success" | "error"

        Returns:
            (records, total): 当前页记录 + 符合条件的总数
        """
        try:
            records = self._load_records()
        except Exception:
            return [], 0

        # 按状态过滤
        if status == "success":
            records = [r for r in records if not r.get("is_error")]
        elif status == "error":
            records = [r for r in records if r.get("is_error")]

        total = len(records)
        # 分页
        records = records[offset:offset + limit]
        return records, total

    def get_by_run_id(self, run_id: str) -> dict | None:
        """按 run_id 查找单条记录。"""
        try:
            records = self._load_records()
        except Exception:
            return None
        for r in records:
            if r.get("run_id") == run_id:
                return r
        return None

    def get_records_since(self, since: datetime) -> list[dict]:
        """获取指定时间之后的记录（用于趋势统计）。"""
        try:
            records = self._load_records()
        except Exception:
            return []
        since_iso = since.isoformat()
        return [r for r in records if r.get("timestamp", "") >= since_iso]

    def _load_records(self) -> list[dict]:
        """从 JSONL 文件加载所有记录。"""
        if not self._path.is_file():
            return []
        records = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 跳过损坏行
        return records

    def _write_records(self, records: list[dict]) -> None:
        """覆盖写入所有记录（原子性不保证，但够用）。"""
        tmp_path = self._path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        # 原子替换（Windows 上 rename 可能不原子，但 tmp 存在时覆盖即可）
        if self._path.exists():
            self._path.unlink()
        tmp_path.rename(self._path)


def build_run_record(prompt: str, persona: str, model: str,
                     result_dict: dict, run_id: str | None = None) -> dict:
    """从 KernelResult dict 构建运行历史记录。

    run_id 可选：传入时用同一 id 与 runs/<id>.jsonl 事件流对齐
    （面板按 run_id 同时查事件回放与 trace）；缺省生成新 uuid。
    """
    meta = result_dict.get("meta") or {}
    trace = meta.get("trace")
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt_preview": prompt[:200] if prompt else "",
        "persona": persona,
        "is_error": bool(result_dict.get("is_error")),
        "error_type": result_dict.get("error_type") or "",
        "cost_usd": float(result_dict.get("cost_usd") or 0.0),
        "duration_ms": int(result_dict.get("duration_ms") or 0),
        "turns": int(result_dict.get("turns") or 0),
        "model": model,
        "has_trace": bool(trace),
        "trace": trace,
    }
