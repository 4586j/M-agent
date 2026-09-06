# 组件登记表 · 原子设计分层

设计与代码的**双向登记**：一行 = 一个可复用组件。开发新组件前必查此表，避免重复造轮子。
分组采用 Atomic Design（原子 → 分子 → 组织 → 模板）：**先用低层组件拼高层，高层不得绕过低层直接实现样式。**

## 登记规则

- 新组件开发完成后**立即**按层级追加一行（G3/G5 卡点勾选项）。
- "代码路径"指向组件实现文件；"设计稿"填 Figma/MasterGo 组件链接（没有则填"-"并在状态列标 `无稿`）。
- 组件名与设计稿中的命名保持一致，作为设计与代码的对应键。
- 修改组件 API 或视觉时更新"状态"与"备注"，并注明日期。
- 组件内部只能引用同层或更低层的组件（分子由原子组成，组织由分子/原子组成）。

## 原子 Atoms（无业务语义的最小单元）

| 组件名 | 代码路径 | 设计稿 | 状态 | 备注 |
|---|---|---|---|---|
| （示例）Button | `src/components/Button.tsx` | figma.com/component/button | stable | 示例行，接入真实项目后可删除；variants: primary/secondary/ghost，尺寸 sm/md/lg |

## 分子 Molecules（原子组成的简单功能组）

| 组件名 | 代码路径 | 设计稿 | 状态 | 备注 |
|---|---|---|---|---|
| TiltCard | `output/blog/index.html`（搜 `function tilt`） | 无稿 | stable | 2026-08-30；3D 倾斜 + 发光描边 + 光泽扫过；仅 pointer:fine + 非 reduced-motion；≤6° |
| Marquee | `output/blog/index.html`（搜 `.marquee-track`） | 无稿 | stable | 2026-08-30；双轨复制法无限滚动，hover 暂停，reduced-motion 降级为折行 |
| TypeLoop | `output/blog/index.html`（搜 `function typing`） | 无稿 | stable | 2026-08-30；打字机多词轮播，reduced-motion 直出首词 |
| Counter | `output/blog/index.html`（搜 `cio`） | 无稿 | stable | 2026-08-30；IO 触发 ease-out 数字滚动，reduced-motion 直出终值 |
| Toast | `output/blog/index.html`（搜 `function toast`） | 无稿 | stable | 2026-09-01；轻提示：ok/err/warn/info 四态（左侧色条）、aria-live 容器、3.4s 自毁；调用方只传文案与类型 |
| ModalFrame | `output/blog/index.html`（搜 `function openModal`） | 无稿 | stable | 2026-09-01；单实例模态框架：mask/Esc/✕ 关闭、焦点落首控件、body 由 build 回调注入；容器置于 main 外（避免透镜克隆）；动态 HTML 必须经 esc() 转义 |

## 组织 Organisms（独立业务区块）

| 组件名 | 代码路径 | 设计稿 | 状态 | 备注 |
|---|---|---|---|---|
| RevealStack | `output/blog/index.html`（搜 `.reveal` 与 `observers`） | 无稿 | stable | 2026-08-30；滚动进入错峰显现系统（IO 统一驱动，仅 transform/opacity，`--d` 控制错峰） |
| StarField | `output/blog/index.html`（搜 `function stars`） | 无稿 | stable | 2026-08-30；Canvas 星空粒子 + 鼠标视差；粒子数 = min(140, w/9)，画布按 clientWidth 取值（防滚动条溢出） |
| ScrollSpyNav | `output/blog/index.html`（搜 `function spy`） | 无稿 | stable | 2026-08-30；玻璃拟态导航 + 滚动位置计算法高亮（必须含 #home 目标，防顶部高亮滞留）+ 进度条 + 汉堡 |
| MagnifierLens | `output/blog/index.html`（搜 `function magnifier`） | 无稿 | stable | 2026-08-30；鼠标放大镜：双层透镜（canvas drawImage 层 + DOM 克隆层 scale(2) 反向平移）；克隆需补 .reveal/.bar-fill/.counter 终态；**克隆必须剥离全部 id**（防重复 id 破坏查询）；仅 pointer:fine + 非 reduced-motion；pointer-events:none 不挡交互；右键开关（禁用原生 contextmenu 是已知权衡） |
| SnakeGame | `output/blog/index.html`（搜 `── 贪吃蛇`） | 无稿 | stable | 2026-08-30；Canvas 网格贪吃蛇：键盘+滑屏、localStorage 最佳分、IO+visibilitychange 双通道自动暂停、方向键 preventDefault 防滚屏、roundRect 兜底 |
| MemoryMatch | `output/blog/index.html`（搜 `── 记忆翻牌`） | 无稿 | stable | 2026-08-30；4×4 翻牌配对：CSS 3D 翻转、失配抖动、步数/最佳 localStorage |
| ReactionTest | `output/blog/index.html`（搜 `── 反应力测试`） | 无稿 | stable | 2026-08-30；反应力测试：idle/waiting/ready/result 状态机、过早判负、ms 成绩 + localStorage 最佳 |
| Game2048 | `output/blog/index.html`（搜 `── 2048`） | 无稿 | stable | 2026-08-30；4×4 压缩合并：slide(row) 纯函数式行处理、方向键+滑屏、按页签门控与贪吃蛇输入互斥、分数/最佳 localStorage、死局 stuck() 双向检查 |
| DesktopPet | `output/blog/index.html`（搜 `F18 桌宠 v2`） | 无稿 | stable | 2026-08-30 v2；全视口游走桌宠：52px emoji 猫 + 加权行为状态机（游走/发呆/坐下/跳舞/跳跃/追鼠标/30s 入睡）+ 朝向翻转 + pointer 拖拽（8px 阈值区分点击，busy 门控暂停行为）+ 爱心/台词反馈 + 🐾 开关持久化 + reduced-motion 静坐可拖拽 |
| PetSinger | `output/blog/index.html`（搜 `F19 唱歌 v2`） | 无稿 | stable | 2026-08-30 v2；桌宠歌单：SONGS 数组 4 曲 round-robin（双击开始/切歌，播放中单击关闭，290ms 双击窗口）、有声曲举 🎤 唱 + LRC 歌词气泡、纯音乐聆听模式（🎧 头戴轻摇摆）、音频资源在 assets/（ADR-004）注意部署需带目录；2026-09-01 增 `__petAddSongs`（曲库注入，标题去重）/`__petStop`（让麦互斥）两钩子 |
| HubClient | `output/blog/index.html`（搜 `博客后端联调层`） | 无稿 | stable | 2026-09-01；后端契约消费层（单文件页面的 fetch 封装）：统一 `{code,message,data,trace_id}` 解包、业务码→异常对象、401 自动 refresh 重试一次、令牌/用户分离存储（契约 refresh 响应不含 user）、离线快速失败（4s 记忆）+ 健康探测短超时；Windows 环回拒绝连接 ~2s，离线断言采样需 ≥3s |
| AuthPanel | `output/blog/index.html`（搜 `function openAuth`） | 无稿 | stable | 2026-09-01；登录/注册双 tab 模态 + 导航登录态 chip（下拉：退出；is_admin 增留言箱入口）；发码按钮按契约 resend_after 倒计时；错误码服务端 message 直显 |
| SongDesk | `output/blog/index.html`（搜 `function openSongDesk`） | 无稿 | stable | 2026-09-01；点歌台：曲库试听（公开，MiniPlayer 播前 `__petStop` 让麦互斥）+ 搜索点歌（仅登录，游客锁引导注册）；点歌反馈含"已在曲库"幂等语义 |
| ContactInbox | `output/blog/index.html`（搜 `function openContact`） | 无稿 | stable | 2026-09-01；联系站长表单（邮箱+验证码+5~500 字，无需登录）+ 管理员留言箱（open/closed 过滤、附注 ≤200 字关闭）；两面板同属留言域 |
| RandomChat | `output/blog/index.html`（搜 `F25 随机聊天`） | 无稿 | stable | 2026-09-02；匿名随机聊天（后端契约 v2.2 §5，WS 帧协议）：idle→waiting→chatting 状态机渲染、25s 心跳+pong 应答、typing 2s 节流、右下角气泡+面板（未读角标、收起不丢帧）、仅登录渲染（renderAuth→syncChatAuth 钩子）、kicked/4401 清会话；服务端匿名昵称"过客A/B"，消息不落库 |
| CareActionBar | `output/pet-client/src/20-body.html`（#actions）+ `src/60-ui.html`（renderActions） | 无稿 | stable | 2026-09-05；照顾循环动作条（喂食/清洁/陪玩）：服务端 `cooldowns` 剩余秒为唯一数据源（本地仅递减显示）、CD 置灰+倒计时+底部进度填充、体力不足预判文案、动作结果 delta 浮层；预判常量来自契约表并注明"以服务端为准" |
| PetStage | `output/pet-client/src/50-game.html`（createStage/drawPet/paintItem） | 无稿 | stable | 2026-09-05；Canvas 分层宠物舞台：挂点表 Z 序（bg→back→本体→body→head→hand，head 锚点必须在眼线之上）、程序生成占位美术（3 species+18 部件按 resource_key 尾号剥层查表）、换装=替换映射不重建、rAF+400ms 看门狗双通道帧调度（部分嵌入环境 rAF 从不触发）、隐藏页 setTimeout 兜底；颜色经 getComputedStyle 读 tokens CSS 变量（单一事实源） |
| MockContractServer | `output/pet-client/src/40-mock.html`（window.PetMock） | 无稿 | stable | 2026-09-05；浏览器内契约 Mock（后端未就绪时前端并行开发的可复用模式）：逐条实现冻结契约的 REST+WS 帧+错误码+幂等/乐观锁/CD/惰性结算；双人=双标签页（BroadcastChannel 房间广播 + **操作前重读共享库**防整库写回互相覆盖）；**会话必须存 sessionStorage**（localStorage 跨标签页共享会互相顶号）；带 url 参数开关（?mock=0 切真实） |

## 模板与页面 Templates & Pages（整页级）

| 组件名 | 代码路径 | 设计稿 | 状态 | 备注 |
|---|---|---|---|---|

---

状态取值：`draft`（开发中）→ `stable`（可用）→ `deprecated`（弃用，注明替代者）。

## 命名约定

- 原子/分子用通用名（Button、Input、SearchField）；组织与页面加业务前缀（Order-Table、User-Detail-Page）。
- 组件内部的 variants（尺寸/颜色/形态）写在"备注"列，保持一行一组件。
- 页面类型的整体布局模式（区块如何拼）不在本表登记，查 `patterns/page-patterns.md`。

## 变更记录

- 2026-09-06 v1.5：新增 `github-ui-resources.md`——GitHub 优质 UI 组件素材库（15 个分类、100+ 组件库，含选型速查表）。
- 2026-09-05 v1.4：pet-client 项目回写——组织 +2（PetStage、MockContractServer）、分子 +1（CareActionBar）；来源：双人宠物养成客户端（按后端冻结契约实现，见 output/pet-client/04-走查报告.md）。
- 2026-09-02 v1.3：组织 +1（RandomChat，来源：blog 附录 G 随机聊天联调）。
- 2026-09-01 v1.2：blog 后端联调功能族登记——分子 +2（Toast、ModalFrame）、组织 +4（HubClient、AuthPanel、SongDesk、ContactInbox）、PetSinger 补两钩子备注（来源：blog 附录 F）。
- 2026-08-30 v1.1：按 Atomic Design 分四层重构（来源：原子设计落地实践——可组合性 + tokens 一致性）；新增"只用低层拼高层"规则。
- 2026-08-30 v1.0：初版单表。
