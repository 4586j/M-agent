"""Read-only architecture review probes; only temporary stores and fake runners.

These probes record current behavior, including defects, rather than asserting
that current behavior is the desired contract. No model or external HTTP calls.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'worker'))
sys.path.insert(0, str(ROOT / 'workspace/agent/orchestrator/runtime'))
os.environ['AGENT_ENV_FILE'] = str(ROOT / '.tmp/review-no-env')

from task_store import TaskStore, TaskRecord
from worker_registry import WorkerRegistry, WorkerInfo
from async_dispatcher import AsyncRemoteDispatcher

results = {}

def task_record(task_id='task-review', status='running'):
    return TaskRecord(task_id, 'executor', 'review fixture', 'worker-review',
                      status, '1', '1')

def registry():
    reg = WorkerRegistry()
    reg.register(WorkerInfo('worker-review', 'executor', '127.0.0.1', 1, {}, 0))
    return reg

async def finalize_race(root):
    store = TaskStore(str(root / 'race'))
    store.write(task_record())
    entered, release = asyncio.Event(), asyncio.Event()
    async def http(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'status_code': 200, 'json': {'content': 'fixture'}}
    spent = []
    disp = AsyncRemoteDispatcher(store, registry(), http,
        product_check=lambda _: {'verdict': 'pass'}, on_spend=spent.append)
    data = {'status': 'done', 'output': 'ok', 'artifacts': ['report.txt'],
            'meta': {'cost_usd': 1}}
    pending = asyncio.create_task(disp.handle_callback('task-review', data))
    await entered.wait()
    store.update('task-review', status='cancelled')
    release.set()
    await pending
    results['cancel_during_finalize'] = {'expected': 'cancelled',
                                        'actual': store.get('task-review').status}
    # Both callbacks enter the artifact await before either finalizes.
    store.write(task_record('task-double'))
    entered.clear()
    release.clear()
    a = asyncio.create_task(disp.handle_callback('task-double', data))
    b = asyncio.create_task(disp.handle_callback('task-double', data))
    await entered.wait()
    await asyncio.sleep(0)
    before = len(spent)
    release.set()
    await asyncio.gather(a, b)
    results['duplicate_callback_spend'] = {'expected_entries': 1,
                                          'actual_entries': len(spent) - before}

async def early_callback(root):
    store = TaskStore(str(root / 'early'))
    async def http(method, url, **kwargs):
        if method == 'POST':
            await disp.handle_callback(kwargs['json']['task_id'],
                                       {'status': 'done', 'output': 'fast'})
        return {'status_code': 200, 'json': {}}
    disp = AsyncRemoteDispatcher(store, registry(), http, poll_interval=999)
    tid = await disp.dispatch_async('executor', 'fast fixture')
    results['callback_before_post_returns'] = {'expected': 'done',
                                               'actual': store.get(tid).status}
    bg = disp.get_background_task(tid)
    if bg is not None:
        bg.cancel()
        await asyncio.gather(bg, return_exceptions=True)

async def memory_scope(root):
    from agent_core.config import AgentConfig
    from agent_core.tools import _guarded_run, _TOOL_SPECS, _make_local_backend
    from agent_core.tool_guard import _tool_cache
    spec = next(s for s in _TOOL_SPECS if s['name'] == 'read_core_memory')
    cfgs = []
    for name in ('A', 'B'):
        base = root / name
        cfg = AgentConfig(memory_core_dir=str(base / 'core'),
            memory_context_dir=str(base / 'ctx'), experience_store_dir=str(base / 'exp'))
        backend = _make_local_backend(cfg)
        await backend['write_core_memory']('same-name', f'PROJECT_{name}')
        cfgs.append((cfg, backend))
    _tool_cache.clear()
    first = await _guarded_run(cfgs[0][0], spec, cfgs[0][1], {'name': 'same-name'})
    second = await _guarded_run(cfgs[1][0], spec, cfgs[1][1], {'name': 'same-name'})
    results['memory_cache_scope'] = {'expected_second_contains': 'PROJECT_B',
        'actual_second': json.loads(second['content'][0]['text']),
        'same_as_first': first == second}
    _tool_cache.clear()

def worker_duplicate(root):
    from fastapi.testclient import TestClient
    from worker_api import create_worker_app
    from worker_config import WorkerConfig
    from agent_runner import TaskResult
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    calls = []
    class Runner:
        def run(self, req):
            with lock:
                calls.append(req.task_id)
                if len(calls) == 2:
                    two_started.set()
            release.wait(3)
            return TaskResult(req.task_id, 'done', output='fixture')
    cfg = WorkerConfig(work_dir=str(root / 'worker'), worker_panel=False,
                       api_key='review-fixture', auto_register=False)
    app = create_worker_app(Runner(), cfg)
    with TestClient(app) as client:
        headers = {'X-API-Key': 'review-fixture'}
        body = {'task_id': 'task-duplicate', 'prompt': 'fixture'}
        a = client.post('/task', headers=headers, json=body)
        b = client.post('/task', headers=headers, json=body)
        two_started.wait(2)
        results['worker_duplicate_post'] = {'http_statuses': [a.status_code, b.status_code],
            'expected_runner_calls': 1, 'actual_runner_calls': len(calls)}
        release.set()

def restarted_registry():
    reg = WorkerRegistry()
    known = reg.heartbeat('worker-before-restart', 0)
    results['heartbeat_after_registry_reset'] = {
        'registration_required': known is False,
        'registered_after_heartbeat': reg.get_worker('worker-before-restart') is not None}

def budget_scheduler(root):
    from budget_store import BudgetStore
    from schedule_store import ScheduleStore, ScheduleRecord
    from task_scheduler import TaskScheduler
    from dispatcher import DispatchResult
    from dispatch_context import AdmittedDispatcher
    budget = BudgetStore(str(root / 'budget.json'))
    budget.set_limits(global_limit_usd=1)
    budget.record_spend(1)
    schedules = ScheduleStore(str(root / 'schedules'))
    schedules.write(ScheduleRecord('schedule-review', 'fixture', '* * * * *',
                                  'executor', 'fixture'))
    calls = []
    class Dispatcher:
        def dispatch(self, **kwargs):
            calls.append(kwargs)
            return DispatchResult('task-scheduled', 'done')
    guarded = AdmittedDispatcher(Dispatcher(), budget,
        lambda _: {'verdict': 'pass'}, TaskStore(str(root / 'schedule-tasks')))
    scheduler = TaskScheduler(schedules, guarded)
    scheduler.trigger_manual('schedule-review')
    results['schedule_budget_gate'] = {'budget_allows': budget.check()[0],
        'dispatch_calls': len(calls),
        'note': 'Scheduler uses the same admitted dispatcher as the Brain application.'}

def retry_lineage(root):
    from dispatcher import RemoteDispatcher
    from retry_manager import RetryManager
    store = TaskStore(str(root / 'retry'))
    reg = registry()
    def fail_http(method, url, **kwargs):
        return {'status_code': 200, 'json': {'status': 'failed', 'error': 'fixture'}}
    dispatcher = RemoteDispatcher(store, reg, fail_http, max_retries=1)
    dispatcher.dispatch('executor', 'fixture')
    retry = RetryManager(store, reg, dispatcher, retry_base_delay=0)
    counts = [len(store.list_all())]
    for _ in range(4):
        retry._retry_eligible()
        counts.append(len(store.list_all()))
    results['retry_lineage_limit_one'] = {'task_counts_after_sweeps': counts,
        'retryable_tasks_after_four_sweeps': len(store.list_retryable()),
        'expected_total_max_attempts_for_one_logical_task': 2}

async def main():
    with tempfile.TemporaryDirectory(prefix='arch-review-') as temp:
        root = Path(temp)
        await finalize_race(root)
        await early_callback(root)
        await memory_scope(root)
        worker_duplicate(root)
        restarted_registry()
        budget_scheduler(root)
        retry_lineage(root)
    results['ci_missing_modules'] = {
        name: importlib.util.find_spec('agent_core.' + name) is not None
        for name in ('execution_host', 'isolated_executor', 'tool_policy')}
    workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
    results['ci_obsolete_imports_removed'] = all(
        'import agent_core.' + name not in workflow and ', agent_core.' + name not in workflow
        for name in ('execution_host', 'isolated_executor', 'tool_policy'))
    print(json.dumps(results, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
