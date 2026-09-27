"""批量执行器（Batch Runner）。

并行执行多个 prompt，汇总结果。使用线程池限制并发数。
"""
import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class BatchItem:
    """批量执行中的单个任务项。"""
    prompt: str
    persona: str = ""
    cwd: str = ""
    context_memory: str = ""
    item_id: str = ""

    def __post_init__(self):
        if not self.item_id:
            self.item_id = str(uuid.uuid4())


@dataclass
class BatchResult:
    """单个任务的执行结果。"""
    item_id: str
    result: dict | None = None
    error: str | None = None
    duration_ms: int = 0


class BatchRunner:
    """批量执行器。

    使用线程池并行执行多个 prompt，限制并发数避免过载。
    """

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max(1, max_concurrent)

    def run_batch(
        self,
        items: list[BatchItem],
        executor_func: Callable[[BatchItem], dict],
    ) -> dict[str, Any]:
        """批量执行。

        Args:
            items: 任务项列表
            executor_func: 执行函数 (BatchItem) -> dict（KernelResult 格式）

        Returns:
            {
                "results": [...],
                "summary": {
                    "total": N,
                    "success": N,
                    "failed": N,
                    "total_cost_usd": X,
                    "total_duration_ms": X,
                }
            }
        """
        results: list[BatchResult] = []

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            future_to_item = {
                pool.submit(executor_func, item): item
                for item in items
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result_dict = future.result()
                    results.append(BatchResult(
                        item_id=item.item_id,
                        result=result_dict,
                        duration_ms=int(result_dict.get("duration_ms") or 0),
                    ))
                except Exception as e:
                    logger.warning("批量任务 %s 失败: %s", item.item_id, e)
                    results.append(BatchResult(
                        item_id=item.item_id,
                        error=str(e),
                    ))

        # 汇总统计
        success_count = sum(1 for r in results if r.error is None)
        failed_count = len(results) - success_count
        total_cost = 0.0
        total_duration = 0

        for r in results:
            if r.result:
                try:
                    total_cost += float(r.result.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                total_duration += r.duration_ms

        return {
            "results": [
                {
                    "item_id": r.item_id,
                    "result": r.result,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                }
                for r in results
            ],
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": failed_count,
                "total_cost_usd": round(total_cost, 6),
                "total_duration_ms": total_duration,
            },
        }

    async def run_batch_async(
        self,
        items: list[BatchItem],
        executor_func: Callable[[BatchItem], dict],
    ) -> dict[str, Any]:
        """异步批量执行（在线程池中运行同步批量执行）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.run_batch, items, executor_func
        )
