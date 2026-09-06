# ADR-002：随机聊天在 blog-backend 原生实现，erp-chat 不整体移植

- 日期：2026-09-02
- 状态：accepted

## 背景

用户希望把自己的聊天组件 `D:\al\erp\heyan\erp-chat`（Java/Spring Boot + MyBatis + MySQL 的成熟**客服型**聊天系统）移入博客，但需求是**随机配对聊天**：系统把在线登录用户自动两两配对，无人可配时提示没人。博客后端栈为 NestJS + SQLite + Redis（ADR-001）。

## 决策

随机聊天在 blog-backend 内**原生实现**（NestJS WebSocket 网关 + 内存等待队列）；**不部署、不改造 erp-chat 服务本体**，而是移植它久经考验的设计：type 化 JSON 帧协议、25s 心跳/90s 判僵尸、分层限流、2000 字上限、右下角气泡 + 面板的嵌入体验（前端参考 `widget.js` 复刻）。

## 备选方案（为什么没选）

- **部署 erp-chat 并改造支持随机配对**：其核心是"访客↔坐席"客服模型（会话分配、坐席台、webhook），改造成 P2P 随机配对需重写 LiveChatService 核心；且引入 JVM + MySQL 双重新运维负担，与单机博客栈（ADR-001）冲突。
- **erp-chat 当引擎、blog-backend 当壳（每个用户同时当 widget 和 agent）**：语义 Hack，归属校验/会话路由全部要绕，脆弱且不可维护。
- **Socket.IO 全家桶**：博客规模不需要房间/命名空间生态，原生 WS（@nestjs/platform-ws）更轻、帧协议完全自定义。

## 后果

- 正面：单一技术栈与鉴权体系（复用 JWT）；帧协议对 erp-chat 风格兼容，前端可借鉴其组件经验；内存队列在个人博客并发下零运维。
- 代价：erp-chat 的附件/撤回/历史落库等能力本期不带入（随机聊天为临时会话，消息不持久化——匿名场景取向）；日后若要持久化/多实例扩展，演进路径是 Redis 队列 + Pub/Sub（架构已留口）。
- 影响范围：03-契约新增 §5 聊天域（v2.2）；前端聊天组件按新帧协议实现，UI 风格参考 erp-chat widget.js。
