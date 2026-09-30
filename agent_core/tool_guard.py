"""工具调用守卫：输入预检、超时控制、结果缓存、自动重试、结构化日志。

包装工具调用流程：
  调用前 → schema 预检 + 查缓存
  调用中 → asyncio.wait_for 超时控制
  调用后 → 写缓存 + 记录 ToolCallRecord
  失败时 → 记忆工具自动重试 1 次，外部 MCP 工具只记录不重试
"""
import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from agent_core.tool_response_guard import validate_tool_response

log = logging.getLogger(__name__)

# 记忆工具名（失败时自动重试）
_MEMORY_TOOLS = frozenset({
    "search_experiences", "read_core_memory",
    "write_core_memory", "write_task_context",
})


@dataclass
class ToolCallRecord:
    """单次工具调用的结构化记录。"""

    tool_name: str
    input_hash: str
    duration_ms: int = 0
    is_error: bool = False
    error_type: str = ""
    cached: bool = False
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "input_hash": self.input_hash[:16],
            "duration_ms": self.duration_ms,
            "is_error": self.is_error,
            "error_type": self.error_type,
            "cached": self.cached,
            "timestamp": self.timestamp,
        }


def _hash_input(tool_name: str, args: dict) -> str:
    """对工具输入参数做确定性哈希（用于缓存键和日志标识）。"""
    try:
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        raw = repr(args)
    return hashlib.sha256(raw.encode()).hexdigest()


class _ToolCache:
    """简单的 LRU 缓存，带 TTL 过期。线程不安全（仅 async 单线程使用）。"""

    def __init__(self, max_size: int = 128, default_ttl: int = 300):
        self._store: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl

    def get(self, key: str) -> object | None:
        """命中且未过期返回值；否则返回 None。"""
        if key not in self._store:
            return None
        expire_at, value = self._store[key]
        if time.monotonic() > expire_at:
            del self._store[key]
            return None
        # 移到末尾（LRU）
        self._store.move_to_end(key)
        return value

    def put(self, key: str, value: object, ttl: int | None = None) -> None:
        """写入缓存。超过 max_size 时淘汰最久未使用的条目。"""
        expire_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (expire_at, value)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def invalidate(self, prefix: str | None = None) -> int:
        """使读缓存失效（写动作提交后调用）。prefix=None 清空全部，
        否则只清除 cache_key 以 prefix 开头的条目。返回被清除的条目数。

        对应方案 Slice 3「Cache only scoped read results; invalidate on writes」：
        写入后相关的陈旧读缓存必须失效，避免后续读到过期快照。
        """
        if prefix is None:
            n = len(self._store)
            self._store.clear()
            return n
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            del self._store[k]
        return len(keys)


# 模块级缓存实例（进程内共享）
_tool_cache = _ToolCache()


def invalidate_tool_cache(prefix: str | None = None) -> int:
    """使进程内共享的工具读缓存失效（供策略层在写动作提交后调用）。

    prefix=None 清空全部读缓存；否则只清除以 prefix 开头的条目（如按工具名
    或资源前缀定向失效）。返回被清除的条目数。
    """
    return _tool_cache.invalidate(prefix)


def validate_tool_input(tool_name: str, args: dict | None,
                        schema: dict | None = None) -> list[str]:
    """工具输入参数预检。返回问题列表（空 = 通过）。

    检查项：
    - args 非 None 且为 dict
    - schema 提供时，校验必填字段存在
    """
    issues = []
    if args is None:
        args = {}
    if not isinstance(args, dict):
        issues.append(f"工具 {tool_name} 参数类型错误：期望 dict，实际 {type(args).__name__}")
        return issues
    if schema and isinstance(schema, dict):
        for key, type_hint in schema.items():
            if key not in args:
                # 有默认值的可选参数不报错（schema 中 type 含 None 表示可选）
                origin = getattr(type_hint, "__origin__", None)
                if origin is not None:
                    continue  # Union/Optional 类型跳过
                if type_hint is type(None):
                    continue
            # 类型检查（仅做基础类型）
            if key in args and isinstance(type_hint, type):
                val = args[key]
                if val is not None and not isinstance(val, type_hint):
                    issues.append(
                        f"工具 {tool_name} 参数 '{key}' 类型错误："
                        f"期望 {type_hint.__name__}，实际 {type(val).__name__}"
                    )
    return issues


async def guarded_tool_call(tool_name: str, handler, args: dict | None,
                            schema: dict | None = None,
                            timeout: int = 30,
                            cache_ttl: int = 300,
                            guard_enabled: bool = True,
                            response_validation: bool = True
                            ) -> tuple[dict, ToolCallRecord]:
    """带守卫的工具调用包装器。

    返回 (result_dict, record)。result_dict 是 MCP 格式的工具返回值。
    record 是本次调用的结构化记录。

    流程：
    1. 输入预检（guard_enabled 时）
    2. 查缓存（命中直接返回）
    3. 执行（asyncio.wait_for 超时控制）
    4. 失败重试（记忆工具 1 次，其他不重试）
    5. 写缓存 + 记录
    6. 工具响应结构校验（guard_enabled 且 response_validation 时，仅告警不阻断）
    """
    args = args or {}
    input_hash = _hash_input(tool_name, args)
    record = ToolCallRecord(
        tool_name=tool_name,
        input_hash=input_hash,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    # 1. 输入预检
    if guard_enabled and schema:
        issues = validate_tool_input(tool_name, args, schema)
        if issues:
            record.is_error = True
            record.error_type = "input_validation"
            return {
                "content": [{"type": "text",
                             "text": f"工具 {tool_name} 输入校验失败：{'; '.join(issues)}"}],
                "is_error": True,
            }, record

    # 2. 查缓存（仅读操作缓存）
    cache_key = f"{tool_name}:{input_hash}"
    if guard_enabled and not _is_write_tool(tool_name):
        cached = _tool_cache.get(cache_key)
        if cached is not None:
            record.cached = True
            return cached, record

    # 3. 执行（超时控制）
    start = time.monotonic()
    max_attempts = 2 if tool_name in _MEMORY_TOOLS else 1
    result = None
    last_error = None

    for attempt in range(max_attempts):
        try:
            result = await asyncio.wait_for(handler(args), timeout=timeout)
            record.is_error = False
            last_error = None
            break
        except asyncio.TimeoutError:
            record.is_error = True
            record.error_type = "timeout"
            last_error = f"工具 {tool_name} 调用超时（>{timeout}s）"
            log.warning("工具调用超时：%s (attempt %d/%d)", tool_name,
                        attempt + 1, max_attempts)
        except Exception as e:  # noqa: BLE001
            record.is_error = True
            record.error_type = type(e).__name__
            last_error = f"工具 {tool_name} 调用失败：{e}"
            log.warning("工具调用失败：%s (attempt %d/%d): %r",
                        tool_name, attempt + 1, max_attempts, e)

    record.duration_ms = int((time.monotonic() - start) * 1000)

    # 4. 构造结果
    if record.is_error:
        result = {
            "content": [{"type": "text", "text": last_error or "未知错误"}],
            "is_error": True,
        }
    elif result is None:
        result = {
            "content": [{"type": "text", "text": f"工具 {tool_name} 返回空结果"}],
            "is_error": True,
        }
        record.is_error = True
        record.error_type = "empty_result"

    # 5. 写缓存（仅成功的读操作）
    if guard_enabled and not record.is_error and not _is_write_tool(tool_name):
        _tool_cache.put(cache_key, result, ttl=cache_ttl)

    # 6. 工具响应验证（仅记录警告，不阻断）
    if guard_enabled and response_validation and not record.is_error \
            and isinstance(result, dict):
        try:
            rv = validate_tool_response(tool_name, result)
            if rv.warnings:
                log.info("工具 %s 响应警告：%s", tool_name, "; ".join(rv.warnings))
                record.error_type = f"response_warning:{'; '.join(rv.warnings[:2])}"
        except Exception:  # noqa: BLE001
            pass  # 验证异常不影响结果

    return result, record


def _is_write_tool(tool_name: str) -> bool:
    """判断工具是否为写操作（写操作不缓存）。"""
    return tool_name.startswith("write_") or tool_name.startswith("delete_")
