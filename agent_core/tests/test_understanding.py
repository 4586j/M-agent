import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.understanding import QuestionGenerator


@pytest.mark.parametrize("result_text", ['{"questions":[]}', None])
@pytest.mark.parametrize("discussion", [False, True])
def test_generator_has_fresh_context_no_tools_and_no_project_kernel(monkeypatch, result_text, discussion):
    calls = []
    class Result:
        result = result_text
        is_error = False
        total_cost_usd = 0.001
        usage = {"input_tokens": 10}
        subtype = 'success'
        session_id = 'isolated-sdk-session'
    def options(**kwargs):
        calls.append(kwargs)
        return kwargs
    class Client:
        def __init__(self, *, options):
            self.options = options
        async def __aenter__(self):
            return self
        async def query(self, prompt):
            calls.append({"prompt": prompt})
            assert Path(self.options["cwd"]).is_dir()
        async def receive_response(self):
            yield SimpleNamespace(model='fake-model',content=[SimpleNamespace(text='{"questions":[]}'),SimpleNamespace(thinking='private thinking')])
            yield Result()
        async def __aexit__(self, *_):
            assert Path(self.options["cwd"]).is_dir(), "client must disconnect before deleting its cwd"
            calls.append({"stream_closed": True})
    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', SimpleNamespace(
        ClaudeAgentOptions=options, ResultMessage=Result, ClaudeSDKClient=Client))
    config = SimpleNamespace(model="fake-model", provider_base_url="", provider_auth_token="")
    source = [{"id":"requirement","content":"需求片段"}]
    generator = QuestionGenerator(config)
    context = {"question": {"id": "Q2"}, "message": "解释当前题", "history": []}
    result = (generator.discuss(context, max_cost_usd=0.05) if discussion else generator(source, max_cost_usd=0.05))
    assert json.loads(calls[1]["prompt"]) == (context if discussion else {"sources":source})
    assert ("不能跳题" in calls[0]["system_prompt"]) is discussion
    assert calls[0]["tools"] == [] and calls[0]["mcp_servers"] == {}
    assert calls[0]["strict_mcp_config"] is True and calls[0]["setting_sources"] == []
    assert calls[0]["max_turns"] == 1 and calls[0]["max_budget_usd"] == 0.05
    assert calls[0]["thinking"] == {"type":"disabled"}
    assert "resume" not in calls[0] and "continue_conversation" not in calls[0]
    assert not Path(calls[0]["cwd"]).exists()
    assert calls[-1] == {"stream_closed": True}
    assert result["cost_usd"] == 0.001
    assert result["raw"] == '{"questions":[]}'
