"""ID 与 host 安全校验。防止路径穿越与 SSRF。"""
import ipaddress
import os
import re

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")
_HOSTNAME = re.compile(
    r"^(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*$"
)


def validate_id(value: str, label: str = "id") -> str:
    """校验 ID 仅含安全字符（字母、数字、下划线、连字符），拒绝路径穿越。"""
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"invalid {label}: {value!r}")
    if not _SAFE_ID.match(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def validate_host(value: str, label: str = "host") -> str:
    """校验 host 为合法 IPv4 / [IPv6] / 域名，拒绝 URL、凭据、穿越等注入形态。

    私网/环回/链路本地地址默认放行（跨主机部署本就是内网形态）；
    设 ORCH_BLOCK_PRIVATE_HOSTS=1 可拒绝，用于要求 worker 必须在公网的场景。
    IPv6 仅接受方括号形式（如 [::1]），因为各调用点以 http://{host}:{port} 拼 URL。
    """
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError(f"invalid {label}: {value!r}")
    if value != value.strip() or value != value.rstrip("."):
        raise ValueError(f"invalid {label}: {value!r}")
    v = value.rstrip(".")
    if v.startswith("[") and v.endswith("]"):
        try:
            ip = ipaddress.ip_address(v[1:-1])
        except ValueError:
            raise ValueError(f"invalid {label}: {value!r}") from None
    else:
        try:
            ip = ipaddress.ip_address(v)
        except ValueError:
            ip = None
        if ip is None:
            if any(c in v for c in ":/@\\?") or ".." in v or v == ".":
                raise ValueError(f"invalid {label}: {value!r}")
            if not _HOSTNAME.match(v):
                raise ValueError(f"invalid {label}: {value!r}")
            return v
        if ip.version == 6:
            raise ValueError(f"IPv6 host 须用方括号形式: {value!r}")
    if ip.is_unspecified or ip.is_multicast:
        raise ValueError(f"unusable {label}: {value!r}")
    # ::1 同时命中 is_reserved（::/8 保留段），仅环回豁免
    if ip.is_reserved and not ip.is_loopback:
        raise ValueError(f"unusable {label}: {value!r}")
    if (ip.is_private or ip.is_loopback or ip.is_link_local) and _block_private():
        raise ValueError(f"private {label} not allowed: {value!r}")
    return v


def _block_private() -> bool:
    return os.environ.get("ORCH_BLOCK_PRIVATE_HOSTS", "").strip().lower() in ("1", "true", "yes")
