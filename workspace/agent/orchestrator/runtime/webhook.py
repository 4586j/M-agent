"""关键事件外发（webhook）：任务待审批 / 预算耗尽 / 节点熔断 / 任务终态。

解决「任务卡在 awaiting_approval 只有面板在线才能看到」的运维盲区：配置
ORCH_WEBHOOK_URL 后，brain 把关键事件 POST 到外部通道（企业微信/钉钉/Slack
网关/自建接收器），操作者不必盯面板。

设计红线：
- **绝不阻断主流程**：外发在独立线程池执行；发送失败只记日志。
- **带重试**：连接错误 / 5xx / 429 指数退避重试；其余 4xx 不重试（调用方错误）。
- **可过滤**：ORCH_WEBHOOK_EVENTS 逗号分隔事件名白名单（空 = 默认全集）。
- **可校验**：ORCH_WEBHOOK_SECRET 非空时携带 X-Webhook-Secret 头，接收方验签。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

log = logging.getLogger(__name__)

# 默认外发事件全集（ORCH_WEBHOOK_EVENTS 过滤用事件名）
DEFAULT_WEBHOOK_EVENTS = frozenset({
    "task_terminal",       # 任务终态（done/failed/lost/cancelled）
    "awaiting_approval",   # 安全拦截挂起待审批
    "budget_exceeded",     # 预算总闸拦截
    "breaker_open",        # worker 连续失败触发熔断
})

# 值得重试的状态码（服务端临时故障/限流）；其余 4xx 是调用方问题，重试无意义
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class WebhookNotifier:
    """线程安全的 webhook 外发器（fire-and-forget）。

    Args:
        url: 目标 URL（http/https）。
        secret: 非空时携带 X-Webhook-Secret 头。
        events: 允许外发的事件名集合；None = DEFAULT_WEBHOOK_EVENTS。
        timeout / max_retries / backoff: 单次请求超时与重试参数（指数退避）。
        http_post: 可注入的 POST 实现 ``(url, json=, headers=, timeout=) ->
            (status_code, error|None)``，默认用 requests；测试注入 fake。
    """

    def __init__(self, url: str, secret: str = "", events=None,
                 timeout: float = 5.0, max_retries: int = 3,
                 backoff: float = 0.5,
                 http_post: Callable[..., tuple] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._url = url
        self._secret = secret
        self._events = frozenset(events) if events is not None \
            else DEFAULT_WEBHOOK_EVENTS
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._backoff = backoff
        self._http_post = http_post or self._default_http_post
        self._sleep = sleep
        # 低事件量场景 2 线程足够；队列有界风险低（payload 小、失败即弃）
        self._pool = ThreadPoolExecutor(max_workers=2,
                                        thread_name_prefix="webhook")

    # ---- 对外 API ----

    def submit(self, event: str, data: dict) -> None:
        """异步外发（入线程池，立即返回；事件不在白名单则跳过）。"""
        if not self._enabled_for(event):
            return
        try:
            self._pool.submit(self._deliver_with_logging, event, data)
        except Exception as e:  # noqa: BLE001 - 线程池异常不拖垮主流程
            log.warning("webhook 提交失败 event=%s: %s", event, e)

    def send(self, event: str, data: dict) -> bool:
        """同步外发（带重试）。返回是否最终成功；仅供测试/特殊路径使用。"""
        if not self._enabled_for(event):
            return False
        return self._deliver(event, data)

    def close(self) -> None:
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass

    # ---- 内部 ----

    def _enabled_for(self, event: str) -> bool:
        return bool(self._url) and (not self._events or event in self._events)

    def _payload(self, event: str, data: dict) -> dict:
        return {"event": event, "timestamp": time.time(), "data": data or {}}

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-Webhook-Secret"] = self._secret
        return headers

    def _deliver_with_logging(self, event: str, data: dict) -> None:
        try:
            if not self._deliver(event, data):
                log.warning("webhook 外发最终失败 event=%s url=%s",
                            event, self._url)
        except Exception as e:  # noqa: BLE001 - 兜底：绝不让线程异常逃逸
            log.warning("webhook 外发异常 event=%s: %r", event, e)

    def _deliver(self, event: str, data: dict) -> bool:
        last_error = ""
        for attempt in range(self._max_retries + 1):
            status_code, error = self._http_post(
                self._url, json=self._payload(event, data),
                headers=self._headers(), timeout=self._timeout)
            if error is None and 200 <= status_code < 300:
                return True
            last_error = error or f"HTTP {status_code}"
            retryable = error is not None or status_code in _RETRYABLE_STATUS
            if not retryable or attempt >= self._max_retries:
                break
            self._sleep(self._backoff * (2 ** attempt))
        log.warning("webhook 外发失败 event=%s（%d 次尝试）: %s",
                    event, self._max_retries + 1, last_error)
        return False

    @staticmethod
    def _default_http_post(url, json=None, headers=None, timeout=None):
        try:
            import requests
            resp = requests.post(url, json=json, headers=headers,
                                 timeout=timeout)
            return resp.status_code, None
        except Exception as e:  # noqa: BLE001 - 网络/超时统一按可重试错误返回
            return 0, str(e)
