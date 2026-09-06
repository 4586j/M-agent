# 可复用模块登记表（Registry）

写任何模块/中间件/工具函数前先查本表（宪法 P2 复用优先），禁止重复实现。
新建或改进模块后按积累规则追加/更新登记（AGENTS.md 第 4 节）。

**分层规则（只上不下）**：接口层只用应用/领域层，应用层只调领域层与基础设施接口，领域层不依赖任何框架与基础设施。新增模块必须标注所属层。

| 层 | 说明 |
|---|---|
| 接口层 | HTTP 路由、参数校验、响应包裹、鉴权中间件 |
| 应用层 | 用例编排、事务边界、跨领域协调 |
| 领域层 | 实体、聚合、业务规则、领域服务 |
| 基础设施层 | 数据库访问、缓存、消息、第三方客户端、工具函数 |

---

## 登记表

<!-- 条目格式：
| 编号 | 模块名 | 层 | 用途一句话 | 位置（仓库/文件） | 依赖 | 登记日期 | 变更原因（更新时必填） |
-->

| 编号 | 模块名 | 层 | 用途一句话 | 位置 | 依赖 | 登记日期 | 变更原因 |
|---|---|---|---|---|---|---|---|
| R-001 | 统一响应包裹拦截器 + 全局异常过滤器 | 接口层 | 成功/失败响应统一 `{code,message,data,trace_id}`，业务码集中定义 | output/blog-backend/src/common/（errors.ts、envelope.interceptor.ts、all-exceptions.filter.ts、trace.middleware.ts） | conventions/response-and-errors.json | 2026-09-01 | blog-backend 首建；后续项目可直接复制四件套。**2026-09-05 pet-backend 复用**（第二个项目）：四件套已复制到 output/pet-backend/src/common/，errors.ts 按 conventions v1.3 扩展 7 个新码并补全 HTTP 状态映射；未用 @RawResponse（本项目无二进制流接口） |
| R-002 | 邮箱验证码服务 | 领域层 | 哈希存储/冷却/每日上限/尝试锁定/阅后即焚的完整验证码能力 | output/blog-backend/src/captcha/（含 9 个单测） | RedisService、MailerService | 2026-09-01 | blog-backend 首建；调研笔记 20260901 邮箱验证码实践的实现化。**2026-09-05 pet-backend 复用**（第二个项目）：output/pet-backend/src/captcha/，场景改为 `pet_login`、Redis key 前缀改为 `pet:`（场景隔离）、每日计数改用 **UTC 日界**（原版本用本地时区，跨时区用户刷新时点不一致）；10 个单测全绿 |
| R-003 | Redis 封装 | 基础设施层 | setEx/incrWithTtl/incrDaily（自动补 TTL）等常用操作 | output/blog-backend/src/redis/redis.service.ts | ioredis | 2026-09-01 | blog-backend 首建。**2026-09-05 pet-backend 扩展复用**：output/pet-backend/src/redis/redis.service.ts 在原能力上新增两类东西——① `safe*` 系列（safeGet/safeSetEx/safeDel/safeTtl/safeIncrWithTtl）吞异常并返回兜底值，使「缓存/锁不可用时业务仍正确」（spec 边界要求）；② `tryAcquireLock` 返回 `{acquired, degraded}`，Redis 挂掉时 `degraded=true` 并**放行**，正确性交给 DB 乐观锁；③ `tryMark`（SET NX）区分 new/duplicate/unavailable 三态供幂等键使用。连接失败不再阻塞启动（原版快速抛错）。**这两个变体已分化，后续项目需先选定语义再复制** |
| R-004 | 下游服务防腐层客户端 | 基础设施层 | 超时/错误码归一（401 配置错误与不可用区分）的下游代理封装 | output/blog-backend/src/music/music-client.service.ts | axios | 2026-09-01 | blog-backend 首建；对接音乐平台。pet-backend 未复用（无下游服务） |
| R-006 | WebSocket 房间网关三件套（RoomGateway 基类 + RoomRegistry + RoomEventPublisher） | 接口层 | 长连接鉴权/心跳判活/僵尸剔除/限长限速/房间表/帧广播，参数口径已线上验证 | output/pet-backend/src/realtime/（room.gateway.ts、room-registry.ts、room-event.publisher.ts、room.module.ts） | ws、GameConfig | 2026-09-05 | 从 blog-backend `random-chat.gateway.ts` **抽取通用能力**而来（4401 关闭码/25s ping/90s 判僵尸/2000 字节限长/20 条每分钟限速，口径原样沿用）。这是该模式的**第二个复用方 = 模式升级信号**，已同时回写 patterns 第 8 组。三处关键改进：① `RoomRegistry.leave()` 校验「登记的确实是这个连接」才移除，避免被顶掉的旧连接误删新连接；② 清理走**两条路径**（close 事件 + `evictStale` 心跳超时），移动网络静默断开常不触发 close；③ RoomRegistry/Publisher 独立为零依赖的 `RoomModule`（@Global），用于打断 Pet ↔ Realtime 循环依赖 |
| R-007 | 时间驱动状态结算器（PetSettleService + 纯函数 settleDecay/settleRecover） | 领域层 | 由「参考值 + 结算时刻 + 速率」用闭式公式 O(1) 算出当前值，支持衰减与恢复两类 | output/pet-backend/src/pet/settle.service.ts（含 19 个单测） | 无（纯函数 + GameConfig） | 2026-09-05 | pet-game 首建（ADR-005）。适用于养成衰减/体力恢复/会员到期/积分过期/放置收益等一切「离线也在变」的场景，已回写 patterns 第 9 组。三个必需防护：① 时钟回拨时 dt=0 + WARN（宁可不变化也不让数值倒涨）；② **时间戳为空/不可解析时 dt=0**（否则 `toMillis('')=0` 会被当成 1970 年，算出几十年 dt 把数值瞬间清零）；③ API 边界才取整，库内存 Float（反复取整回写会漂移） |

---

## 待登记（模块尚未落地，不得提前登记）

| 预定编号 | 模块名 | 所属侧 | 登记时机 |
|---|---|---|---|
| R-005 | 客户端平台适配层 `IPolicy`（登录/存储/socket/剪贴板抽象 + Web 实现 + 微信预留） | 前端侧（`D:\agent\web\output\pet-client`） | pet-game T-30 完成后由前端侧登记 |

---

## 积累原则

- 新增条目用新编号追加；更新既有条目时保留"变更原因"一行。
- 永不删除历史记录；废弃模块标注 `已废弃→R-xxx` 而非删除。
- 模块被第二个项目复用时，在"变更原因"记录复用情况（这是模式升级的信号，可考虑进 `patterns/`）。
