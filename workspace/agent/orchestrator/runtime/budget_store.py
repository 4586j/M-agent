"""全局/每日预算闸门：brain 层成本总闸。

单任务预算（task_budget_usd）只约束一次执行；本模块在 brain 派发层加总闸：
- global_limit_usd：累计花费上限（跨天）
- daily_limit_usd：当日（本地时区自然日）花费上限
- 账本 budgets.json（runtime 目录，gitignore）：已花费总额 + 按日累计

花费来源：任务回传 meta.cost_usd（sync/async/local 三条链路终态时经
on_spend 钩子上报）。超限后 check() 返回拒绝理由，/dispatch 与
/dispatch/async 的前置关卡据此 429，阻断新派发；上限可经 PUT /budgets 热调。

0 或 None 上限一律视为「不限制」（与 task_budget_usd 语义一致）。
"""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# 账本按日桶只保留近 N 天（老桶裁剪，防无限膨胀）
_DAY_BUCKET_KEEP = 60


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _valid_limit(v) -> float | None:
    """上限归一化：None/<=0 → None（不限制）；其余转 float。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    v = float(v)
    return v if v > 0 else None


class BudgetStore:
    """线程安全的预算账本（JSON 持久化，原子写）。"""

    def __init__(self, budgets_file: str, now_fn=time.time):
        self._path = Path(budgets_file)
        self._now = now_fn
        # RLock：set_limits 持锁期间调用 snapshot()（重入），普通 Lock 会死锁
        self._lock = threading.RLock()
        self._data: dict = {
            "global_limit_usd": None,   # None = 不限制
            "daily_limit_usd": None,
            "spent_total_usd": 0.0,
            "spent_by_day": {},         # {"YYYY-MM-DD": float}
            "updated_at": 0.0,
        }
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data.update(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # 账本损坏：从零计数而非拒绝服务，但必须告警（预算可能被低估）
            log.warning("预算账本 %s 解析失败（%s），已从零重建", self._path, e)

    def _save(self) -> None:
        """原子落盘（tmp + os.replace），与 experience_store 同模式。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".budgets-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._path)
        except OSError as e:
            log.warning("预算账本写入失败 %s: %s", self._path, e)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    # ---- 对外 API ----

    def set_limits(self, global_limit_usd=None, daily_limit_usd=None) -> dict:
        """热调上限（0/None=不限制）。返回更新后的快照。"""
        with self._lock:
            self._data["global_limit_usd"] = _valid_limit(global_limit_usd)
            self._data["daily_limit_usd"] = _valid_limit(daily_limit_usd)
            self._data["updated_at"] = self._now()
            self._save()
            return self.snapshot()

    def record_spend(self, cost_usd) -> None:
        """累计一笔花费（任务终态回传 meta.cost_usd）。非法/非正值忽略。"""
        if not isinstance(cost_usd, (int, float)) or isinstance(cost_usd, bool) \
                or cost_usd <= 0:
            return
        with self._lock:
            self._data["spent_total_usd"] = \
                round(self._data["spent_total_usd"] + float(cost_usd), 6)
            day = _today()
            by_day = self._data.setdefault("spent_by_day", {})
            by_day[day] = round(by_day.get(day, 0.0) + float(cost_usd), 6)
            self._prune_days(by_day)
            self._data["updated_at"] = self._now()
            self._save()

    def check(self) -> tuple[bool, str]:
        """预算闸门判定：返回 (allowed, reason)。reason 仅在拒绝时非空。"""
        with self._lock:
            g = _valid_limit(self._data["global_limit_usd"])
            d = _valid_limit(self._data["daily_limit_usd"])
            total = self._data["spent_total_usd"]
            today = self._data["spent_by_day"].get(_today(), 0.0)
            if g is not None and total >= g:
                return False, (f"全局预算已用尽：已花 ${total:.4f} / "
                               f"上限 ${g:.2f}（可在「任务」页调整预算）")
            if d is not None and today >= d:
                return False, (f"当日预算已用尽：今日已花 ${today:.4f} / "
                               f"上限 ${d:.2f}（可在「任务」页调整预算）")
            return True, ""

    def snapshot(self) -> dict:
        """预算面板视图：上限 + 已用 + 剩余（None=不限制）。"""
        with self._lock:
            g = _valid_limit(self._data["global_limit_usd"])
            d = _valid_limit(self._data["daily_limit_usd"])
            total = round(self._data["spent_total_usd"], 6)
            today = round(self._data["spent_by_day"].get(_today(), 0.0), 6)
            return {
                "global_limit_usd": g,
                "daily_limit_usd": d,
                "spent_total_usd": total,
                "spent_today_usd": today,
                "global_remaining_usd": round(g - total, 6) if g is not None else None,
                "daily_remaining_usd": round(d - today, 6) if d is not None else None,
                "updated_at": self._data["updated_at"],
            }

    @staticmethod
    def _prune_days(by_day: dict) -> None:
        """按日桶裁剪：只保留近 _DAY_BUCKET_KEEP 天。"""
        if len(by_day) <= _DAY_BUCKET_KEEP:
            return
        cutoff = time.time() - _DAY_BUCKET_KEEP * 86400
        for k in [k for k in by_day
                  if _day_ts(k) is not None and _day_ts(k) < cutoff]:
            del by_day[k]


def _day_ts(day: str) -> float | None:
    """YYYY-MM-DD → 时间戳；非法格式返回 None。"""
    try:
        return datetime.strptime(day, "%Y-%m-%d").timestamp()
    except ValueError:
        return None
