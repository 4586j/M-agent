"""dispatcher 与 async_dispatcher 共享的工具函数。"""
from pathlib import Path

from experience_store import ExperienceStore

_TEXT_ARTIFACT_EXT = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".sh",
                      ".ts", ".tsx", ".js", ".jsx", ".csv", ".log", ".ini",
                      ".toml", ".env", ".xml", ".html", ".css"}


def is_text_artifact(path: str) -> bool:
    return Path(path).suffix.lower() in _TEXT_ARTIFACT_EXT


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
