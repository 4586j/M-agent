"""定时任务调度持久化。ScheduleRecord + ScheduleStore + 轻量 cron 解析器。"""
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from safe_id import validate_id


@dataclass
class ScheduleRecord:
    schedule_id: str
    name: str
    cron_expr: str
    role: str
    prompt: str
    context_memory: str | None = None
    model: str | None = None
    provider: str | None = None  # provider 名（触发时按当前环境再解析；不含密钥）
    enabled: bool = True
    last_run: str = ""
    next_run: str = ""
    created_at: str = ""


class ScheduleStore:
    def __init__(self, store_dir: str):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._cache: list[ScheduleRecord] | None = None

    def _invalidate(self) -> None:
        self._cache = None

    def _path(self, schedule_id: str) -> Path:
        validate_id(schedule_id, "schedule_id")
        return self.store_dir / f"{schedule_id}.json"

    def write(self, record: ScheduleRecord) -> None:
        with open(self._path(record.schedule_id), "w", encoding="utf-8") as f:
            json.dump(asdict(record), f, ensure_ascii=False, indent=2)
        self._invalidate()

    def get(self, schedule_id: str) -> ScheduleRecord | None:
        if self._cache is not None:
            for r in self._cache:
                if r.schedule_id == schedule_id:
                    return r
            return None
        p = self._path(schedule_id)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return ScheduleRecord(**json.load(f))

    def delete(self, schedule_id: str) -> bool:
        p = self._path(schedule_id)
        if p.exists():
            p.unlink()
            self._invalidate()
            return True
        return False

    def list_all(self) -> list[ScheduleRecord]:
        if self._cache is not None:
            return list(self._cache)
        out = []
        for p in sorted(self.store_dir.glob("*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(ScheduleRecord(**json.load(f)))
        self._cache = out
        return list(out)

    def list_enabled(self) -> list[ScheduleRecord]:
        return [r for r in self.list_all() if r.enabled]

    def update_last_run(self, schedule_id: str, ts: str) -> ScheduleRecord | None:
        rec = self.get(schedule_id)
        if rec is None:
            return None
        rec.last_run = ts
        self.write(rec)
        return rec

    def update_next_run(self, schedule_id: str, next_run: str) -> ScheduleRecord | None:
        rec = self.get(schedule_id)
        if rec is None:
            return None
        rec.next_run = next_run
        self.write(rec)
        return rec

    def toggle_enabled(self, schedule_id: str) -> ScheduleRecord | None:
        rec = self.get(schedule_id)
        if rec is None:
            return None
        rec.enabled = not rec.enabled
        self.write(rec)
        return rec


# ── Cron 解析器 ──

_INTERVAL_RE = re.compile(r"^@interval_(\d+)s$")


def parse_cron(expr: str) -> tuple[int, int, int, int, int] | int:
    """解析 cron 表达式。

    支持：
    - 5 字段 cron: minute hour day month weekday
    - @interval_Ns: 每 N 秒（返回秒数）

    5 字段 cron 每个字段支持：数字、*、*/N、逗号分隔列表、范围 A-B
    """
    m = _INTERVAL_RE.match(expr)
    if m:
        return int(m.group(1))

    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron 表达式需要 5 个字段，收到 {len(parts)}: {expr!r}")

    result = []
    for i, part in enumerate(parts):
        lo, hi = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)][i]
        result.append(_parse_field(part, lo, hi))
    return tuple(result)


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """解析单个 cron 字段，返回允许值的集合。"""
    values: set[int] = set()
    for part in field.split(","):
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"步长必须 > 0: {part}")
            if base == "*":
                start = lo
            elif "-" in base:
                start = int(base.split("-", 1)[0])
            else:
                start = int(base)
            values.update(range(start, hi + 1, step))
        elif part == "*":
            values.update(range(lo, hi + 1))
        elif "-" in part:
            a, b = part.split("-", 1)
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    return {v for v in values if lo <= v <= hi}


def compute_next_run(cron_expr: str, after_ts: float) -> float:
    """计算 cron_expr 在 after_ts 之后的下一次触发时间（Unix 时间戳）。

    @interval_Ns → after_ts + N
    5 字段 cron → 逐分钟扫描（上限 48h）
    """
    import time as _time

    interval = parse_cron(cron_expr)
    if isinstance(interval, int):
        return after_ts + interval

    minutes, hours, days, months, weekdays = interval

    # 从下一分钟开始扫描
    t = after_ts - (after_ts % 60) + 60
    limit = after_ts + 48 * 3600

    while t <= limit:
        lt = _time.gmtime(t)
        if (lt.tm_min in minutes and lt.tm_hour in hours and
                lt.tm_mday in days and lt.tm_mon in months and
                lt.tm_wday in weekdays):
            return t
        t += 60

    raise RuntimeError(f"无法在 48h 内找到匹配: {cron_expr!r}")
