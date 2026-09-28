import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedule_store import ScheduleStore, ScheduleRecord, parse_cron, compute_next_run
from task_scheduler import TaskScheduler
from dispatcher import DispatchResult


def make_schedule(sid="sched-1", cron_expr="@interval_60s", enabled=True,
                  next_run="", last_run=""):
    return ScheduleRecord(
        schedule_id=sid, name="test", cron_expr=cron_expr,
        role="executor", prompt="do x",
        enabled=enabled, next_run=next_run, last_run=last_run,
        created_at="100",
    )


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, role, prompt, context_memory=None, model=None,
                 provider=None):
        self.calls.append({"role": role, "prompt": prompt,
                           "context_memory": context_memory, "model": model,
                           "provider": provider})
        return DispatchResult(task_id="task-new", status="done")

    def redispatch(self, task_id, model=None, provider=None):
        return self.dispatch(role="executor", prompt="r")


# ── Cron parser ──

def test_parse_interval():
    assert parse_cron("@interval_300s") == 300


def test_parse_cron_every_minute():
    result = parse_cron("* * * * *")
    assert isinstance(result, tuple)
    assert len(result) == 5
    assert result[0] == set(range(60))
    assert result[1] == set(range(24))


def test_parse_cron_specific_time():
    result = parse_cron("30 9 * * *")
    assert result[0] == {30}
    assert result[1] == {9}


def test_parse_cron_step():
    result = parse_cron("*/15 * * * *")
    assert result[0] == {0, 15, 30, 45}


def test_parse_cron_range():
    result = parse_cron("0 9-17 * * 1-5")
    assert result[1] == {9, 10, 11, 12, 13, 14, 15, 16, 17}
    assert result[4] == {1, 2, 3, 4, 5}


def test_parse_cron_invalid_fields():
    try:
        parse_cron("* *")
        assert False, "应抛错"
    except ValueError:
        pass


def test_compute_next_run_interval():
    assert compute_next_run("@interval_60s", 1000.0) == 1060.0


def test_compute_next_run_cron():
    import time
    base = 1700000000.0
    nxt = compute_next_run("* * * * *", base)
    assert nxt > base
    assert nxt - base <= 120


# ── ScheduleStore ──

def test_write_and_get(tmp_path):
    store = ScheduleStore(str(tmp_path))
    rec = make_schedule()
    store.write(rec)
    got = store.get("sched-1")
    assert got is not None
    assert got.name == "test"
    assert got.cron_expr == "@interval_60s"


def test_get_missing(tmp_path):
    store = ScheduleStore(str(tmp_path))
    assert store.get("nope") is None


def test_delete(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule())
    assert store.delete("sched-1") is True
    assert store.get("sched-1") is None
    assert store.delete("sched-1") is False


def test_list_all_and_enabled(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", enabled=True))
    store.write(make_schedule("s-2", enabled=False))
    store.write(make_schedule("s-3", enabled=True))
    assert len(store.list_all()) == 3
    assert len(store.list_enabled()) == 2


def test_toggle_enabled(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule(enabled=True))
    rec = store.toggle_enabled("sched-1")
    assert rec.enabled is False
    rec2 = store.toggle_enabled("sched-1")
    assert rec2.enabled is True


def test_update_last_run(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule())
    rec = store.update_last_run("sched-1", "999")
    assert rec.last_run == "999"


def test_update_next_run(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule())
    rec = store.update_next_run("sched-1", "1234")
    assert rec.next_run == "1234"


# ── TaskScheduler ──

def test_tick_triggers_due_schedule(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", cron_expr="@interval_60s", next_run="100"))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    triggered = sched.tick()
    assert triggered == ["s-1"]
    assert len(fake_disp.calls) == 1
    assert fake_disp.calls[0]["role"] == "executor"


def test_tick_skips_future_schedule(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", cron_expr="@interval_60s", next_run="300"))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    triggered = sched.tick()
    assert triggered == []
    assert len(fake_disp.calls) == 0


def test_tick_skips_disabled_schedule(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", cron_expr="@interval_60s",
                              enabled=False, next_run="100"))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    triggered = sched.tick()
    assert triggered == []


def test_tick_updates_last_run_and_next_run(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", cron_expr="@interval_60s", next_run="100"))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    sched.tick()
    rec = store.get("s-1")
    assert rec.last_run == "200"
    assert rec.next_run != ""
    assert float(rec.next_run) > 200


def test_trigger_manual(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", cron_expr="@interval_3600s", next_run="99999"))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, now_fn=lambda: 200.0, ts_fn=lambda: "200")

    ok = sched.trigger_manual("s-1")
    assert ok is True
    assert len(fake_disp.calls) == 1


def test_trigger_manual_unknown(tmp_path):
    store = ScheduleStore(str(tmp_path))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp)
    assert sched.trigger_manual("nope") is False


def test_init_next_runs(tmp_path):
    store = ScheduleStore(str(tmp_path))
    store.write(make_schedule("s-1", cron_expr="@interval_60s", next_run=""))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, now_fn=lambda: 1000.0, ts_fn=lambda: "1000")

    sched._init_next_runs()
    rec = store.get("s-1")
    assert rec.next_run != ""
    assert float(rec.next_run) > 1000


def test_start_and_stop(tmp_path):
    store = ScheduleStore(str(tmp_path))
    fake_disp = FakeDispatcher()
    sched = TaskScheduler(store, fake_disp, check_interval=1)

    sched.start()
    assert sched._thread is not None
    assert sched._thread.daemon is True

    sched.stop()
    sched._thread.join(timeout=3)
    assert not sched._thread.is_alive()
