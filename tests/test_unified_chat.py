import json
import time

import httpx
import pytest

from scripts.unified_chat_brain import ChatBackend, UnifiedChatBrain, chat_body, parse_choice
from wikirace import unified_protocol as p
from wikirace.eval import _api_usage
from wikirace.state import Candidate, PageRef, RaceState


def response(content='{"answers":{"next":{"choice":"A"}}}', finish="stop"):
    return {"model": "models/gemini-3.8-flash", "choices": [{"finish_reason": finish,
             "message": {"content": content}}], "usage": {"prompt_tokens": 11, "completion_tokens": 10, "total_tokens": 25}}


@pytest.mark.parametrize("content,finish", [
    ('{"answers":{"next":{"choice":"Z"}}}', 'stop'),
    ('{"answers":{"next":{"choice":"A","explanation":"x"}}}', 'stop'),
    ('```json\n{"answers":{"next":{"choice":"A"}}}\n```', 'stop'),
    ('{"answers":{"next":{"choice":"A"}}}', 'length'),
    ('[]', 'stop'),
])
def test_invalid_or_incomplete_choice_never_becomes_a_click(content, finish):
    with pytest.raises(ValueError):
        parse_choice(response(content, finish), ["A", "B"])


def test_chat_body_preserves_exact_rules_evidence_and_255_options():
    options = [p.option({"id": str(i), "title": f"Article {i}"}) for i in range(255)]
    item = p.decision({"goal": {"title": "Goal"}, "current": {"title": "Start"}}, options)
    request = {"model": "test", "state": item["state"], "questions": p.choice_questions(item)}
    body = chat_body(request)
    assert body["messages"][0]["content"].startswith(p.INSTRUCTION)
    payload = json.loads(body["messages"][1]["content"])
    assert payload["evidence"] == item["state"]
    assert payload["options"] == [{"code": o["code"], "description": o["description"]} for o in item["options"]]
    assert len(payload["options"]) == 255
    assert body["model"] == "test"


def test_transport_deadline_rejects_call_before_network():
    calls = []
    backend = ChatBackend(client=httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))), api_key="test")
    backend.set_deadline(time.perf_counter() - 1)
    with pytest.raises(TimeoutError):
        backend.post({})
    assert calls == []
    backend.close()


def test_report_can_read_progress_while_next_line_is_being_written(tmp_path):
    from scripts.report_gemini_comparison import latest_progress
    path = tmp_path / "progress.jsonl"
    path.write_text('{"clicks_so_far": 7}\n{"clicks_so_far":')
    assert latest_progress(path) == {"clicks_so_far": 7}


def test_shared_tournament_usage_and_wire_inputs_are_auditable():
    pytest.importorskip("tokenizers")
    from scripts.summarize_unified_comparison import audit_call
    wire = []
    def respond(request):
        assert request.headers["Authorization"] == "Bearer secret-placeholder"
        wire.append(json.loads(request.content))
        return httpx.Response(200, json=response())
    backend = ChatBackend(client=httpx.Client(transport=httpx.MockTransport(respond)), api_key="secret-placeholder")
    brain = UnifiedChatBrain(backend=backend)
    state = RaceState(goal=PageRef(title="Goal"), current=PageRef(title="Start"), observation_mode="page",
                      candidates=[Candidate(id=str(i), title=f"Page {i}") for i in range(256)])
    action, debug = brain.choose_action(state)
    assert action.link_id == "0"
    assert [cid for group in debug["unified_rounds"][0] for cid in group["ids"]] == [str(i) for i in range(256)]
    for call, sent in zip(debug["unified_calls"], wire):
        audit_call(call)
        assert call["chat_request"] == sent == chat_body(call["request"])
    usage = _api_usage(debug)
    assert usage["requests"] == 2 and usage["input_tokens"] == 22
    assert "secret-placeholder" not in json.dumps(debug)
    brain.close()
