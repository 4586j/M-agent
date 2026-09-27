"""任务模板存储单元测试。"""
import tempfile
from pathlib import Path

import pytest

from agent_core.template_store import TemplateStore


@pytest.fixture
def tmp_store_dir(tmp_path):
    return str(tmp_path / "templates")


@pytest.fixture
def store(tmp_store_dir):
    return TemplateStore(tmp_store_dir)


class TestTemplateStore:
    def test_create_template(self, store):
        t = store.create("greeting", "Hello, {name}!", description="问候模板")
        assert t["name"] == "greeting"
        assert t["template"] == "Hello, {name}!"
        assert t["variables"] == ["name"]
        assert t["description"] == "问候模板"
        assert t["created_at"]
        assert t["updated_at"]

    def test_create_duplicate_raises(self, store):
        store.create("test", "template 1")
        with pytest.raises(ValueError, match="已存在"):
            store.create("test", "template 2")

    def test_get_template(self, store):
        store.create("test", "Hello {name}")
        t = store.get("test")
        assert t is not None
        assert t["name"] == "test"

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None

    def test_list_templates(self, store):
        store.create("t1", "Template 1")
        store.create("t2", "Template 2")
        templates = store.list()
        assert len(templates) == 2
        names = {t["name"] for t in templates}
        assert names == {"t1", "t2"}

    def test_update_template(self, store):
        store.create("test", "Old: {x}")
        updated = store.update("test", template="New: {y}")
        assert updated is not None
        assert updated["template"] == "New: {y}"
        assert updated["variables"] == ["y"]
        assert updated["updated_at"] > updated["created_at"]

    def test_update_nonexistent(self, store):
        assert store.update("nonexistent", template="x") is None

    def test_update_description(self, store):
        store.create("test", "Template")
        updated = store.update("test", description="New description")
        assert updated["description"] == "New description"
        assert updated["template"] == "Template"  # 未变

    def test_delete_template(self, store):
        store.create("test", "Template")
        assert store.delete("test") is True
        assert store.get("test") is None

    def test_delete_nonexistent(self, store):
        assert store.delete("nonexistent") is False

    def test_render_basic(self, store):
        store.create("greeting", "Hello, {name}! You are {age} years old.")
        result = store.render("greeting", {"name": "Alice", "age": 30})
        assert result == "Hello, Alice! You are 30 years old."

    def test_render_missing_variable(self, store):
        store.create("test", "Hello {name}, from {city}")
        with pytest.raises(ValueError, match="缺少变量"):
            store.render("test", {"name": "Alice"})

    def test_render_nonexistent_template(self, store):
        with pytest.raises(KeyError, match="不存在"):
            store.render("nonexistent", {})

    def test_render_extra_variables_ignored(self, store):
        store.create("test", "Hello {name}")
        result = store.render("test", {"name": "Alice", "extra": "ignored"})
        assert result == "Hello Alice"

    def test_extract_variables(self, store):
        store.create("test", "Hello {name}, {greeting} from {city}!")
        t = store.get("test")
        assert set(t["variables"]) == {"name", "greeting", "city"}

    def test_extract_variables_dedup(self, store):
        store.create("test", "{x} and {x} and {y}")
        t = store.get("test")
        assert t["variables"] == ["x", "y"]  # 去重保序

    def test_explicit_variables(self, store):
        store.create("test", "Hello {name}", variables=["name", "custom"])
        t = store.get("test")
        assert t["variables"] == ["name", "custom"]

    def test_persistence(self, tmp_store_dir):
        store1 = TemplateStore(tmp_store_dir)
        store1.create("persistent", "Template {var}")

        # 新建 store 实例，应从文件加载
        store2 = TemplateStore(tmp_store_dir)
        t = store2.get("persistent")
        assert t is not None
        assert t["template"] == "Template {var}"

    def test_no_variables(self, store):
        store.create("static", "No variables here")
        t = store.get("static")
        assert t["variables"] == []
        result = store.render("static", {})
        assert result == "No variables here"

    def test_unicode_content(self, store):
        store.create("中文", "你好，{名字}！")
        result = store.render("中文", {"名字": "小明"})
        assert result == "你好，小明！"
