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
