# multi-agent-orchestrator

> 文档类型：当前入口；最后核对：2026-10-18。内容包含当前工作区未提交实现，不代表已发布或已部署版本。

统一 Agent 内核、个人人机协作、多主机执行调度与团队协作基础。`agent_core` 复用模型执行、工具、会话、计划和记忆，不同入口分别承担人员职责、授权和交付流程。

## 项目定位

| 协作方式 | 人的职责 | 当前边界 |
| --- | --- | --- |
| 个人单 Agent | 讨论、理解、参与实现和决策 | CLI、HTTP、面板和内核已实现；理解沟通仅接入个人 HTTP/UI |
| 领导监督多 Agent | 目标和高层计划、预算、监察、批准与验收 | Brain/Worker 已有调度、执行与监控基础；自治项目流程待完善 |
| 多人团队与个人 Agent | 每位成员拥有个人 Agent，经授权按职责协作 | 独立团队服务已有身份、需求、预算和验收协议；真实 Planner/Runner 尚未默认接通 |

协作方式与部署方式分别考虑：一台主机也能运行多个 Agent。个人理解沟通不加入 Brain/Worker 或团队派发链路。

## 文档入口

完整导航见 [文档中心](docs/README.md)。首次接手项目按以下顺序阅读：

1. [项目介绍](docs/项目介绍.md)：目标、模式、能力现状和非目标。
2. [系统架构](docs/系统架构.md)：模块、调用链、数据权威、权限和费用边界。
3. [开发计划](docs/开发计划.md)：优先级、依赖、交付物与验收条件。
4. [问题修复与解决方法](docs/问题修复与解决方法.md)：现象、根因、方法、验证及待办。
5. [开发与文档维护规范](docs/开发与文档维护规范.md)：后续如何持续更新资料。

变更记录见 [CHANGELOG](CHANGELOG.md)，仓库规则见 [AGENTS.md](AGENTS.md)。

## 安装与启动

从仓库根目录执行；Python 3.11+。前端 CI 使用 Node 20，实际依赖声明以仓库文件为准。

```bash
python -m pip install -e "./agent_core[service,dev]"
python -m pip install -r workspace/agent/orchestrator/runtime/requirements.txt
python -m pip install -r worker/requirements.txt
```

个人模式：

```bash
agent
# 或启动个人 HTTP 服务与面板：
python -m agent_core.service --port 8000
```

个人面板：`http://127.0.0.1:8000/panel`。理解沟通见 [个人机制说明](docs/需求理解核验设计与使用说明_2026-10-18.md)；CLI、外部 MCP 本次没有增加这套交互入口。

Brain/Worker 分别在两个终端启动：

```bash
# Brain：终端一，从仓库根目录进入运行时
cd workspace/agent/orchestrator/runtime
python -m brain_api

# Worker：终端二，从仓库根目录进入 worker
cd worker
python worker_api.py
```

Brain 默认端口 7000，Worker 默认端口 8000；与个人服务同机运行时须错开端口。远程派发设置 `ORCH_MODE=remote`；Brain 本机执行另需 `ORCH_LOCAL_EXEC=1`。旧 Worker 默认 `ORCH_RUNNER=fake`，只做模拟执行；真实执行需配置 `ORCH_RUNNER=sdk` 及模型、人设和权限。详见 [使用手册](docs/使用手册.md)，页面启动不能证明真实模型执行已接通。

编排前端：

```bash
npm --prefix web ci
npm --prefix web run dev
npm --prefix web run build
```

团队服务使用独立数据目录与认证，操作见 [开发与运行规范](docs/开发与文档维护规范.md)。配置以 [.env.example](.env.example) 为准；真实密钥不写进文档或提交。

## 验证与评测

```bash
python docs/check_docs.py
python -m pytest agent_core/tests worker/tests workspace/agent/orchestrator/runtime/tests -q
npm --prefix web test
npm --prefix web run build
```

真实 SDK 测试默认跳过；浏览器测试和验证边界见维护规范。测试通过不等于真实业务验收或生产部署。

```bash
python eval/run_eval.py --dry-run
python eval/run_eval.py
python eval/run_eval.py --without-experience
python eval/compare.py eval/results/A.json eval/results/B.json
```

`--dry-run` 只列出任务；普通评测经 Brain 派发，需要服务和模型配置。任务集见 [eval/tasks](eval/tasks)，结果保存在 `eval/results/`。
