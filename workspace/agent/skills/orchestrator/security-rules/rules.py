"""安全规则合并器与加载器。

合并语义（security-guard 在读取时遵循相同语义）：
overrides 中相同 id 的规则覆盖默认，新 id 追加。
兼作结构 lint：load_effective_rules 可被测试/CI 校验所有规则字段完整。
"""
from pathlib import Path
from typing import Any

import yaml

RULES_DIR = Path(__file__).parent


def load_raw(path: Path) -> list[dict[str, Any]]:
    """读取单个 YAML 文件，返回 rules 列表；文件不存在或为空返回空列表。"""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("rules", [])


def merge_rules(default: list[dict], overrides: list[dict]) -> list[dict]:
    """合并默认与覆盖规则：同 id 覆盖，新 id 追加。保留默认顺序，追加项在后。"""
    by_id: dict[str, dict] = {}
    for r in default:
        by_id[r["id"]] = r
    for r in overrides:
        by_id[r["id"]] = r
    return list(by_id.values())


def load_effective_rules() -> list[dict[str, Any]]:
    """加载默认 + 用户覆盖后的生效规则。"""
    default = load_raw(RULES_DIR / "default.yaml")
    overrides = load_raw(RULES_DIR / "overrides" / "user.yaml")
    return merge_rules(default, overrides)


if __name__ == "__main__":
    for r in load_effective_rules():
        print(f"{r['id']}\t{r['severity']}\t{r['category']}\t{r['description']}")
