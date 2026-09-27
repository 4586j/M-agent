"""brain_api POST /translate 端点测试：结果翻译（独立上下文）。

覆盖：正常翻译、target_lang 透传与缺省、translator 未配置、
已是目标语言跳过、翻译失败降级（端点仍 ok，降级信息在 payload）。
用假 TranslatorService，避免真实 LLM 调用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from agent_core.translator import TranslationResult
from brain_api import create_brain_app
from config import Config
from task_store import TaskStore
from worker_registry import WorkerRegistry


class _FakeTranslator:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def translate(self, text, *, target_lang=None, skip_if_target=True):
        self.calls.append({"text": text, "target_lang": target_lang})
        return self._result


def _client(tmp_path, translator=None):
    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    store = TaskStore(str(tmp_path / "tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / "tasks"),
                 security_llm=False)
    app = create_brain_app(reg, store, cfg, translator=translator)
    return TestClient(app)


def test_translate_returns_translation(tmp_path):
    tr = _FakeTranslator(TranslationResult(translated="完成：42 行",
                                           target_lang="zh", changed=True))
    client = _client(tmp_path, translator=tr)
    body = client.post("/translate", json={"text": "Done: 42 rows"}).json()
    assert body["status"] == "ok"
    assert body["translation"]["translated"] == "完成：42 行"
    assert body["translation"]["changed"] is True
    assert body["translation"]["target_lang"] == "zh"
    assert tr.calls[0]["text"] == "Done: 42 rows"


def test_translate_target_lang_passthrough(tmp_path):
    tr = _FakeTranslator(TranslationResult(translated="Done", target_lang="en",
                                           changed=True))
    client = _client(tmp_path, translator=tr)
    client.post("/translate", json={"text": "完成", "target_lang": "en"})
    assert tr.calls[0]["target_lang"] == "en"


def test_translate_target_lang_defaults_none(tmp_path):
    """缺省 target_lang=None：交由翻译服务跟随全局 translate_target_lang。"""
    tr = _FakeTranslator(TranslationResult(translated="完成", target_lang="zh",
                                           changed=True))
    client = _client(tmp_path, translator=tr)
    client.post("/translate", json={"text": "Done"})
    assert tr.calls[0]["target_lang"] is None


def test_translate_not_configured(tmp_path):
    client = _client(tmp_path, translator=None)
    body = client.post("/translate", json={"text": "Done"}).json()
    assert body["status"] == "failed"
    assert "translator" in body["error"]


def test_translate_skipped_reported(tmp_path):
    tr = _FakeTranslator(TranslationResult(translated="已是中文", target_lang="zh",
                                           changed=False, skipped="already_target_lang"))
    client = _client(tmp_path, translator=tr)
    body = client.post("/translate", json={"text": "已是中文"}).json()
    assert body["status"] == "ok"
    assert body["translation"]["skipped"] == "already_target_lang"
    assert body["translation"]["changed"] is False


def test_translate_error_degrades_in_payload(tmp_path):
    """翻译内部失败：端点仍返回 ok（HTTP 成功），降级信息落在 translation.error。"""
    tr = _FakeTranslator(TranslationResult(translated="", target_lang="zh",
                                           changed=False, error="翻译执行失败：boom"))
    client = _client(tmp_path, translator=tr)
    body = client.post("/translate", json={"text": "Done"}).json()
    assert body["status"] == "ok"
    assert body["translation"]["error"] != ""
    assert body["translation"]["changed"] is False
