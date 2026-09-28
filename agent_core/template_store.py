# NOTE: 预留模块（仅被 agent_core/tests 引用，无生产代码引用）。
# 未接入内核的执行路径；若未来启用需接线到 kernel/worker，且删除时
# 仅删本文件与对应测试。是否有必要保留由项目组评估。

"""任务模板存储（Template Store）。

CRUD 管理 prompt 模板，支持变量替换。模板持久化到 JSONL 文件。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TemplateStore:
    """任务模板 CRUD 存储。

    模板结构：
    {
        "name": "csv_analysis",
        "template": "请分析文件 {filename} 的统计信息",
        "variables": ["filename"],
        "created_at": "2026-09-11T...",
        "updated_at": "2026-09-11T..."
    }
    """

    def __init__(self, store_dir: str):
        self._dir = Path(store_dir)
        self._path = self._dir / ".prompt_templates.jsonl"
        self._templates: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """从 JSONL 文件加载模板。"""
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                        name = t.get("name")
                        if name:
                            self._templates[name] = t
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning("template_store: 加载失败: %s", e)

    def _save(self) -> None:
        """保存所有模板到 JSONL 文件。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".jsonl.tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                for t in self._templates.values():
                    f.write(json.dumps(t, ensure_ascii=False) + "\n")
            if self._path.exists():
                self._path.unlink()
            tmp_path.rename(self._path)
        except OSError as e:
            logger.warning("template_store: 保存失败: %s", e)

    def create(
        self,
        name: str,
        template: str,
        variables: list[str] | None = None,
        description: str = "",
    ) -> dict:
        """创建新模板。

        Args:
            name: 模板名（唯一标识）
            template: 模板文本，变量用 {var_name} 占位
            variables: 变量名列表（可选，自动从 template 提取）
            description: 模板描述

        Returns:
            创建的模板 dict

        Raises:
            ValueError: 模板名已存在
        """
        with self._lock:
            if name in self._templates:
                raise ValueError(f"模板名已存在: {name}")

            if variables is None:
                variables = self._extract_variables(template)

            now = datetime.now(timezone.utc).isoformat()
            t = {
                "name": name,
                "template": template,
                "variables": variables,
                "description": description,
                "created_at": now,
                "updated_at": now,
            }
            self._templates[name] = t
            self._save()
            return t

    def get(self, name: str) -> dict | None:
        """获取模板。"""
        with self._lock:
            return self._templates.get(name)

    def list(self) -> list[dict]:
        """列出所有模板。"""
        with self._lock:
            return list(self._templates.values())

    def update(
        self,
        name: str,
        template: str | None = None,
        variables: list[str] | None = None,
        description: str | None = None,
    ) -> dict | None:
        """更新模板。

        Returns:
            更新后的模板 dict，不存在返回 None
        """
        with self._lock:
            t = self._templates.get(name)
            if t is None:
                return None

            if template is not None:
                t["template"] = template
                if variables is None:
                    t["variables"] = self._extract_variables(template)
            if variables is not None:
                t["variables"] = variables
            if description is not None:
                t["description"] = description
            t["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return t

    def delete(self, name: str) -> bool:
        """删除模板。

        Returns:
            True 如果成功删除，False 如果不存在
        """
        with self._lock:
            if name not in self._templates:
                return False
            del self._templates[name]
            self._save()
            return True

    def render(self, name: str, values: dict[str, Any]) -> str:
        """渲染模板，替换变量。

        Args:
            name: 模板名
            values: 变量值字典

        Returns:
            渲染后的文本

        Raises:
            KeyError: 模板不存在
            ValueError: 缺少必要变量
        """
        with self._lock:
            t = self._templates.get(name)
            if t is None:
                raise KeyError(f"模板不存在: {name}")

            template = t["template"]
            variables = t.get("variables", [])

            # 检查必要变量
            missing = [v for v in variables if v not in values]
            if missing:
                raise ValueError(f"缺少变量: {', '.join(missing)}")

            # 替换变量
            result = template
            for key, val in values.items():
                result = result.replace(f"{{{key}}}", str(val))
            return result

    @staticmethod
    def _extract_variables(template: str) -> list[str]:
        """从模板文本中提取变量名。"""
        # 匹配 {var_name} 格式
        matches = re.findall(r"\{(\w+)\}", template)
        return list(dict.fromkeys(matches))  # 去重保序
