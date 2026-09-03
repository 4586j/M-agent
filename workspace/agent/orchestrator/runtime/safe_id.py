"""ID 安全校验。防止路径穿越攻击。"""
import re

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_id(value: str, label: str = "id") -> str:
    """校验 ID 仅含安全字符（字母、数字、下划线、连字符），拒绝路径穿越。"""
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"invalid {label}: {value!r}")
    if not _SAFE_ID.match(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value
