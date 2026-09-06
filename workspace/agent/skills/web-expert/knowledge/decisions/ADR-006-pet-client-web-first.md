# ADR-006：pet-client 采用单文件 HTML + Canvas 2D（偏离后端 ADR-003 的 Cocos Creator 选型）

- 日期：2026-09-05
- 状态：accepted
- 关联：后端仓 `knowledge/decisions/ADR-003-pet-game客户端选CocosCreator.md`（其复评触发条件 ①②）、本仓 `ADR-003-single-file-html.md`（同构先例）

## 背景

后端侧 `05-tasks.md` 阶段 5（T-27~T-38 客户端）移交本仓执行，客户端选型按后端 ADR-003 为 Cocos Creator 3.8.x LTS。实现时发现：

1. **本机未安装 Cocos Creator**（已核查 Program Files / ProgramData / LocalAppData）；编辑器为 GUI 工具且首次启动需账号登录激活，AI 无法自主完成工程创建、场景编辑与 Web 双端构建。
2. 后端 ADR-003 自身预留了复评触发条件：①微信端确认放弃 → 评估切换 Web-first 栈；②性能不达标 → 评估 Phaser/PixiJS 轻量方案。用户已明确「先不做微信小程序」，触发条件 ① 的前提已部分成立。
3. 本 Demo 的验证重心（后端 spec §1）：双人并发一致性、服务端权威结算、分层 Sprite 换装 + 实时广播——全部是网络与状态层能力，与引擎无关；美术按 spec 开放问题 2 走「程序生成占位图」。

## 决策

pet-client 采用**零依赖单文件 `index.html` + Canvas 2D 分层渲染 + 原生 WebSocket**（与本仓 ADR-003-single-file-html 同构），同时**原样保留**后端 `02-架构说明.md` §3.2 的 `core / platform / game / ui` 四层分区与 §3.3 挂点表/Z 序：

- `core/`（HttpClient/SocketClient/SettleModel/EventBus）平台无关，禁止 `document.*`/`wx.*`；
- `platform/` 保留 `WebPolicy` + `WechatPolicy`（空实现、调用即抛），业务零平台判断；
- `game/`（PetStateModel/InteractController/OutfitRenderer 语义）与 `ui/` 与后端文档一一对应。

## 备选方案（为什么没选）

- **坚持 Cocos Creator**：需要人工安装编辑器 + 注册账号 + GUI 操作，阻塞本次交付；T-27/T-38 的完成定义（编辑器打开无报错/构建面板留档）在当前环境不可达。若后续正式版需要，核心逻辑可按分层迁移（见升级路径）。
- **React + Vite（toolchain 默认栈）**：宠物渲染是单 Canvas 舞台 + 少量 HUD，引入框架的组件化收益为负；且与后端文档的四层分区映射变绕。
- **Phaser/PixiJS**：仅为本 Demo 引入渲染引擎依赖，Canvas 2D 原生能力已覆盖（分层绘制/帧动画/粒子），零依赖更符合交付环境。

## 后果

- **正面**：双击即可预览、静态部署即上线（Nginx `/pet/`）；与本仓既有单文件资产（blog）运维方式一致；后端四层架构约定零损耗保留，联调时后端侧文档/心智完全对齐。
- **代价**：放弃 Cocos 的图集合批/DrawCall 控制与「一键补做微信小游戏/原生 App」能力；单文件超 2000 行后维护成本上升（已用 src 分区 + build 拼装缓解）。
- **升级路径**（触发任一即迁 Cocos）：① 确定要微信小游戏/原生端；② 需要正式美术资产与骨骼动画（Spine socket 挂点）；③ 部件数量与图集规模超出 Canvas 逐帧绘制预算。迁移面：仅 `game/` 渲染层与 `platform/`，`core/` 状态机与契约逻辑可直接复用。

## 变更记录

- 2026-09-05 v1：初版。来源：pet-client 实现期，已同步后端 05-tasks 偏移日志与本仓 01-spec §2。
