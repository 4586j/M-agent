# ADR-001：blog-backend 采用 NestJS + Prisma + SQLite

- 日期：2026-09-01
- 状态：accepted

## 背景

blog-backend 为个人博客（`D:\agent\web\output\blog`）提供账号、申请、音乐代理能力。数据规模极小（用户 < 1 万、申请单 < 1 万），部署在博客作者本机/单机，旁边已有同机运行的单机音乐服务（Flask + data.db）。toolchain.json 默认数据库为 PostgreSQL。

## 决策

采用 NestJS + Prisma + SQLite；Redis 为独立依赖（验证码/限流强需求，不可省）。

## 备选方案（为什么没选）

- PostgreSQL：默认项，但本项目需要额外安装运维一个数据库进程，收益（并发/扩展）在个人博客规模下用不到。
- 纯 Redis 持久化（无关系库）：用户/申请单需要关系查询与唯一约束，Redis 不适合。
- 复用音乐服务 data.db：跨服务共享数据库违反边界（patterns 防腐层），拒绝。

## 后果

- 正面：零运维、单文件备份、与既有音乐服务单机风格一致；Prisma 保留日后迁移 PostgreSQL 的能力（改 provider 即可）。
- 代价：SQLite 写并发弱（本项目写 QPS < 1 可忽略）；多机部署时需先迁库。
- 影响范围：toolchain.json database 类的默认值不修改（本 ADR 为项目级例外）；03-契约定义的 user/application 表按 SQLite 类型给出。
