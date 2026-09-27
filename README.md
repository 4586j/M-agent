# multi-agent-orchestrator

统一 Agent 内核 + 集群编排的多 Agent 框架：`agent_core`（单机 CLI / 本地服务 / 集群 worker 共用内核）、`workspace/agent/orchestrator/runtime`（大脑 brain：注册、派发、任务/经验/记忆、security-guard）、`worker`（执行节点）、`web`（控制面板）。

## 启动方式（三选一）

```bash
# 1) 单机 CLI 对话（推荐 editable 安装后用 agent 命令）
pip install -e ./agent_core
agent                       # 或：python agent_cli.py

# 2) 大脑 brain（集群编排；单机执行模式加 ORCH_LOCAL_EXEC=1）
cd workspace/agent/orchestrator/runtime
python -m brain_api         # ORCH_MODE=remote 启用集群派发；默认 local

# 3) worker 执行节点（集群模式，需先启动 brain）
cd worker
python worker_api.py        # 经 ORCH_BRAIN_URL 注册并领任务
```

环境变量（`ORCH_API_KEY` / `ORCH_BRAIN_URL` / `ORCH_PERMISSION_MODE` 等）见 `.env.example` 与 `docs/使用手册.md`。

## 测试

```bash
python -m pytest agent_core/tests worker/tests workspace/agent/orchestrator/runtime/tests -q
```

## 评测（eval/）

```bash
python eval/run_eval.py --dry-run          # 列出任务与判定方式，不执行
python eval/run_eval.py                    # 经 brain /dispatch 派发评测任务集
python eval/run_eval.py --without-experience   # 经验闭环 A/B 对照组
python eval/compare.py eval/results/A.json eval/results/B.json  # 对比两次结果
```

任务集在 `eval/tasks/`（JSON schema 见 `eval/run_eval.py` 模块注释），结果写入 `eval/results/`。

详细使用说明、故障排查与架构文档：[`docs/使用手册.md`](docs/使用手册.md)。
