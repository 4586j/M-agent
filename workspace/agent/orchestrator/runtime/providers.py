"""brain 侧 provider 池（名称 → 模型/端点/密钥）+ 默认 provider 热切。

密钥唯一持久点：providers.json（gitignore，运维文件）。auth_token 支持 ${ENV_VAR}
引用——resolve 时从 os.environ 替换实际值，避免把明文密钥复制进多份存储（任务记录/
审批缓存/审计都不落 token）。环境变量缺值时视该 provider「不可解析」（dispatch 400，
宁缺不装坏的）。

热切语义：PUT /providers/default 只改「后续新任务的默认 provider」；已 running/
排队的任务用已解析值，行为可预期。
"""
import json
import logging
import os
import re
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _mask(token) -> str:
    """遮蔽密钥：只留末 4 位，供 GET /providers 回显（不返回明文）。"""
    t = str(token or "")
    return ("***" + t[-4:]) if len(t) > 4 else "***"


def _resolve_value(text, env) -> tuple[str | None, list[str]]:
    """把 ${VAR} 占位符替换为 env 实值；返回 (值或None, 缺失的变量名列表)。"""
    text = (text or "").strip()
    if not text:
        return None, []
    missing: list[str] = []

    def repl(m):
        val = env.get(m.group(1))
        if val is None or val == "":
            missing.append(m.group(1))
            return m.group(0)
        return val

    return _ENV_REF.sub(repl, text), missing


class ProviderStore:
    """providers.json 的读写 + 默认 provider + 名称解析。线程安全。"""

    def __init__(self, path: str | Path, env: dict | None = None):
        self.path = Path(path)
        self._env = os.environ if env is None else env
        self._lock = Lock()
        self._data = self._load()

    # ---- 持久化 ----

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"default": "", "providers": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # 文件损坏不拖垮启动：按空池重建（不覆盖原文件）
            log.warning("providers.json 解析失败，按空池启动: %s", e)
            return {"default": "", "providers": {}}
        if not isinstance(data, dict):
            return {"default": "", "providers": {}}
        data.setdefault("default", "")
        data.setdefault("providers", {})
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    # ---- 读 ----

    def list_all(self) -> dict:
        """遮蔽密钥后的只读视图：{default, providers: {name: {model, base_url, auth_token(masked)}}}。"""
        with self._lock:
            providers = {}
            for name, p in sorted((self._data["providers"] or {}).items()):
                providers[name] = {
                    "model": p.get("model") or None,
                    "base_url": p.get("base_url") or None,
                    "auth_token": _mask(p.get("auth_token")),
                }
            return {"default": self._data.get("default", ""),
                    "providers": providers}

    def names(self) -> list[str]:
        with self._lock:
            return sorted((self._data["providers"] or {}).keys())

    def default(self) -> str:
        with self._lock:
            return self._data.get("default", "") or ""

    def get_raw(self, name: str) -> dict | None:
        with self._lock:
            return (self._data["providers"] or {}).get(name)

    # ---- 写 ----

    def upsert(self, name: str, *, model: str = "", base_url: str = "",
               auth_token: str = "") -> None:
        with self._lock:
            # 注意：不能用 (dict or {}) mutate——空 dict 是 falsy，盘子会被替换丢弃
            providers = self._data.setdefault("providers", {})
            providers[name] = {
                "model": model or None,
                "base_url": base_url or None,
                "auth_token": auth_token or None,
            }
            self._save()

    def delete(self, name: str) -> bool:
        with self._lock:
            providers = self._data.setdefault("providers", {})
            existed = name in providers
            providers.pop(name, None)
            if self._data.get("default") == name:
                self._data["default"] = ""
            if existed:
                self._save()
            return existed

    def set_default(self, name: str) -> None:
        """热切默认。name 空=清空默认；name 必须存在于池（否则 KeyError）。"""
        with self._lock:
            if name and name not in (self._data["providers"] or {}):
                raise KeyError(name)
            self._data["default"] = name or ""
            self._save()

    # ---- 解析（执行用：${ENV} 替换） ----

    def resolve(self, name: str) -> dict | None:
        """解析 provider 名 → 实际三元组 {name, model, base_url, auth_token}。

        环境变量缺失（${VAR} 取不到值）→ 返回 None（视同不可用，dispatch 侧 400）。
        """
        raw = self.get_raw(name)
        if raw is None:
            return None
        bv, bm = _resolve_value(raw.get("base_url"), self._env)
        tv, tm = _resolve_value(raw.get("auth_token"), self._env)
        if bm or tm:
            missing = sorted(set(bm + tm))
            log.warning("provider '%s' 环境变量不可解析（%s），视为不可用",
                        name, ", ".join(missing))
            return None
        return {"name": name,
                "model": raw.get("model") or None,
                "base_url": bv,
                "auth_token": tv}