"""定时任务调度器。后台线程每分钟检查到期 schedule 并触发 dispatch。"""
import logging
import threading
import time
from typing import Callable

from schedule_store import ScheduleStore, ScheduleRecord, compute_next_run
from dispatcher import Dispatcher

log = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(
        self,
        schedule_store: ScheduleStore,
        dispatcher: Dispatcher,
        now_fn: Callable[[], float] | None = None,
        ts_fn: Callable[[], str] | None = None,
        check_interval: int = 60,
    ):
        self.schedule_store = schedule_store
        self.dispatcher = dispatcher
        self._now = now_fn or time.time
        self._ts = ts_fn or (lambda: str(int(time.time())))
        self.check_interval = check_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._init_next_runs()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.check_interval):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    def tick(self) -> list[str]:
        """一轮检查：触发所有到期的 schedule，返回触发的 schedule_id 列表。"""
        now = self._now()
        triggered = []
        for rec in self.schedule_store.list_enabled():
            if not rec.next_run:
                continue
            try:
                next_ts = float(rec.next_run)
            except (ValueError, TypeError):
                continue
            if now < next_ts:
                continue
            try:
                res = self.dispatcher.dispatch(
                    role=rec.role, prompt=rec.prompt,
                    context_memory=rec.context_memory, model=rec.model,
                )
                self.schedule_store.update_last_run(rec.schedule_id, self._ts())
                self._update_next_run(rec)
                log.info("[trace=%s] schedule=%s dispatched task=%s",
                         res.trace_id, rec.schedule_id, res.task_id)
                triggered.append(rec.schedule_id)
            except Exception:
                log.exception("scheduled dispatch failed for %s", rec.schedule_id)
        return triggered

    def _init_next_runs(self) -> None:
        """启动时为所有缺少 next_run 的 schedule 计算下次触发时间。"""
        now = self._now()
        for rec in self.schedule_store.list_enabled():
            if not rec.next_run:
                try:
                    next_ts = compute_next_run(rec.cron_expr, now)
                    self.schedule_store.update_next_run(rec.schedule_id, str(next_ts))
                except Exception:
                    log.exception("failed to compute next_run for %s", rec.schedule_id)

    def _update_next_run(self, rec: ScheduleRecord) -> None:
        try:
            now = self._now()
            next_ts = compute_next_run(rec.cron_expr, now)
            self.schedule_store.update_next_run(rec.schedule_id, str(next_ts))
        except Exception:
            log.exception("failed to compute next_run for %s", rec.schedule_id)

    def trigger_manual(self, schedule_id: str) -> bool:
        """手动触发一个 schedule。"""
        rec = self.schedule_store.get(schedule_id)
        if rec is None:
            return False
        try:
            res = self.dispatcher.dispatch(
                role=rec.role, prompt=rec.prompt,
                context_memory=rec.context_memory, model=rec.model,
            )
            self.schedule_store.update_last_run(schedule_id, self._ts())
            self._update_next_run(rec)
            log.info("[trace=%s] schedule=%s manual trigger dispatched task=%s",
                     res.trace_id, schedule_id, res.task_id)
            return True
        except Exception:
            log.exception("manual trigger failed for %s", schedule_id)
            return False
