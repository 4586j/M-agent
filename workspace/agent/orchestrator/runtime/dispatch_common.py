"""dispatcher / async_dispatcher / local_executor_dispatcher 共享的工具函数。"""
from pathlib import Path
from typing import Any

from experience_store import ExperienceStore
from memory_store import MemoryStore

_TEXT_ARTIFACT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".sh",
                      ".ts", ".tsx", ".js", ".jsx", ".csv", ".log", ".ini",
                      ".toml", ".env", ".xml", ".html", ".css"}

# 用量上报的权威键子集：kernel 产出 usage dict 时只保留这几项；
# brain/worker/eval 侧据此汇聚 /stats、TaskResult.meta 与评测 token 指标。
# 单一源头——各处一致性随 kernel 变更自动对齐（曾四处复制）。
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens")


def is_text_artifact(path: str) -> bool:
    return Path(path).suffix.lower() in _TEXT_ARTIFACT_EXT


def clamp_context(text: str | None, max_chars: int) -> str:
    """context_memory 限幅：超长截断并标注。

    上下文是注意力预算内的稀缺资源，无界注入会拖垮长任务召回
    （context rot）并放大 token 成本；保头部（约定/规范类内容通常在前）。
    """
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text or ""
    return (f"{text[:max_chars]}\n\n"
            f"[context_memory 已截断：原始 {len(text)} 字符，保留前 {max_chars}]")


def write_result_note(memory_store: MemoryStore | None, task_id: str,
                      status: str, output: Any, error: Any) -> None:
    """任务终态时自动写入上下文记忆 result 笔记。

    让 V2 的上下文记忆落库（此前只有 V1 主会话约定），后续任务可在
    派发时按 task-id 引用这些过程信息。失败静默，不拖垮派发主流程。
    """
    if memory_store is None:
        return
    out_head = str(output)[:800] if output else ""
    lines = [f"status: {status}"]
    if out_head:
        lines.append(f"output(前800字):\n{out_head}")
    if error:
        lines.append(f"error: {str(error)[:300]}")
    try:
        memory_store.write_context(task_id, "result", "\n".join(lines))
    except Exception:
        pass


def assemble_context(memory_store: MemoryStore, base: str | None,
                     task_ids: list[str], per_task_chars: int = 2000) -> str:
    """把引用任务的上下文记忆笔记并入本次派发的 context_memory。

    每个任务一个块，块内各笔记截断到 per_task_chars，整体再由
    clamp_context 限幅。无效 task_id 抛 ValueError（由端点转 400）。
    """
    blocks: list[str] = []
    for tid in task_ids:
        keys = memory_store.list_context(tid)
        if not keys:
            continue
        lines = [f"[历史任务上下文 {tid}]"]
        for k in keys:
            content = memory_store.read_context(tid, k) or ""
            lines.append(f"- {k}: {content[:per_task_chars]}")
        blocks.append("\n".join(lines))
    if not blocks:
        return base or ""
    joined = "\n\n".join(blocks)
    return f"{base}\n\n{joined}" if base else joined


def enrich_experience(experience_store: ExperienceStore | None,
                      prompt: str, role: str,
                      context_memory: str | None) -> str:
    """检索相似历史经验，拼接到 context_memory 供 Worker 参考。"""
    if not experience_store:
        return context_memory or ""
    similar = experience_store.search(prompt, role=role,
                                      successful_only=True, limit=3)
    if not similar:
        return context_memory or ""
    exp_lines = ["[历史经验参考]"]
    for i, exp in enumerate(similar, 1):
        exp_lines.append(f"{i}. 任务: {exp.prompt[:100]}")
        if exp.output_summary:
            exp_lines.append(f"   产出: {exp.output_summary[:200]}")
    exp_block = "\n".join(exp_lines)
    if context_memory:
        return f"{context_memory}\n\n{exp_block}"
    return exp_block


def put_provider_name(meta: dict, provider: dict | None) -> None:
    """把 provider 名写入 meta（密钥绝不进 meta/不落盘）；provider 空则跳过。

    三个派发器在「记录日志/终态合并」时共用的同一语义：TaskRecord.meta 与
    审批缓存只允许出现 provider 的名字，base_url/auth_token 只走瞬时 body。
    """
    if provider and provider.get("name"):
        meta["provider"] = provider["name"]


def provider_body(provider: dict | None) -> dict:
    """把已解析的 provider 三元组转成瞬时 worker body（密钥只过内存）。

    返回值并入派发请求体：provider 名 + 显式给出的 base_url/auth_token。
    为空/未显式声明的字段省略，worker 侧回落进程 env（与 kernel._build_options
    的四层兜底一致）。provider 为空返回空 dict（调用方直接展开，无副作用）。
    """
    if not provider:
        return {}
    body: dict[str, Any] = {"provider": provider.get("name")}
    if provider.get("base_url"):
        body["provider_base_url"] = provider["base_url"]
    if provider.get("auth_token"):
        body["provider_auth_token"] = provider["auth_token"]
    return body
