# ADR-004：音频资源引入 assets/ 目录（修订单文件交付）

- 日期：2026-08-30
- 状态：accepted

## 背景

F19 要求桌宠小猫播放 music 项目的《晴天》。该文件为 55MB FLAC，无法内嵌进单文件 HTML（base64 后约 73MB）；环境无 ffmpeg 无法转码压缩。ADR-003 的"零依赖单文件交付"决策需要修订。

## 决策

交付物从"单文件"扩展为"index.html + assets/ 目录"：
1. `assets/qingtian.flac` — 从 music 项目 downloads 复制的完整音源（无转码，零损耗）。
2. `assets/qingtian-lrc.js` — 由 music 项目 .lrc 解析生成的歌词数据（跳过元信息行），以 `<script>` 引入。

Chrome/Edge/Firefox 均原生支持 FLAC 播放，file:// 与 http:// 下相对路径均可加载。

## 备选方案（为什么没选）

- **base64 内嵌**：55MB → 73MB HTML，页面加载与编辑体验崩溃。放弃。
- **引用原位置**（`../../../../../al/erp/music/...` 相对路径）：脆弱且路径丑陋，music 项目移动即失效。放弃。
- **转码 m4a/mp3 压缩**：环境无 ffmpeg；未来有转码条件时可压缩至 ~5MB 并可选内嵌，届时再修订。

## 后果

- 正面：音质无损、实现最简、music 项目与博客解耦（博客自带音源）。
- 代价：交付物不再是单个文件——**移动或部署需连同 assets/ 目录**；仓库体积 +55MB。
- 影响范围：README"快速开始"说明、部署路径（未来 Vercel/Netlify 直接部署整个目录即可）。
