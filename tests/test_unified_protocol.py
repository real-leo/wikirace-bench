"""Input parity, full-title retention, coverage and actual tokenizer boundaries."""
import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("tokenizers")
from wikirace import unified_protocol as p
from wikirace.unified_brain import UnifiedBrain
from wikirace.eval import _api_usage
from wikirace.state import Candidate, PageRef, RaceState


@pytest.fixture(scope="module")
def planner():
    if not (p.ROOT / "models/Qwen3.5-4B/tokenizer.json").exists():
        pytest.skip("local tokenizer artifacts required")
    return p.Planner()


def state(count=255):
    return RaceState(goal=PageRef(title="Goal", description="Specific target " * 40),
                     current=PageRef(title="Start", description="Current page " * 40),
                     history=["Older", "Start"], observation_mode="page",
                     candidates=[Candidate(id=f"L{i}", title="Goal" if i == count-1 else f"Article {i}",
                                           context="Some local context", url=f"https://en.wikipedia.org/wiki/Article_{i}", score=1.0)
                                 for i in range(count)])


class Backend:
    model = "test"
    def reset_episode(self): pass
    def set_deadline(self, value): pass
    def close(self): pass
    def _post(self, payload):
        choices = payload["questions"]["next"]["criteria"]
        winner = next((code for code, desc in choices.items() if desc.startswith("Goal\n")), next(reversed(choices)))
        return {"answers": {"next": {"choice": winner}}, "usage": {"input_tokens": 10, "output_tokens": 0}}


def test_common_fields_have_one_limit_and_no_score_or_url_hints(planner):
    s = state()
    shared = p.evidence(s)
    assert len(shared["goal"]["description"]) == len(shared["current"]["description"]) == 280
    opts = [p.option(c) for c in s.candidates]
    assert all("url" not in o and "score" not in o for o in opts)
    item = p.decision(shared, opts)
    question = p.choice_questions(item)["next"]
    row = p.semif_row(item)
    assert question["instructions"] == row["question"] == p.INSTRUCTION
    assert list(question["criteria"].values()) == [o["description"] for o in row["options"]]
    assert row["state"] == item["state"]
    audit = planner.audit(item)
    assert audit["fits"] and audit["markers"] == 255


@pytest.mark.parametrize("count", [1, 2, 255, 256, 601])
def test_all_links_participate_and_goal_is_chosen_by_model(planner, count):
    brain = UnifiedBrain("jev", backend=Backend(), planner=planner)
    s = state(count)
    action, debug = brain.choose_action(s)
    assert action.link_id == f"L{count-1}"
    if count == 1:
        assert not debug["unified_calls"]
        return
    previous = [c.id for c in s.candidates]
    for groups in debug["unified_rounds"]:
        assert [cid for g in groups for cid in g["ids"]] == previous
        assert all(g["winner"] in g["ids"] and len(g["ids"]) <= 255 for g in groups)
        previous = [g["winner"] for g in groups]
    assert previous == [action.link_id]
    assert _api_usage(debug)["requests"] == len(debug["unified_calls"])
    assert _api_usage(debug)["input_tokens"] == 10 * len(debug["unified_calls"])


def test_shared_budget_splits_without_truncating_long_titles(planner):
    s = state(255)
    # Longer than upstream Laya's implicit 48-token per-option cap.
    options = [p.option({"id":c.id, "title": ("Long distinctive article title " * 20) + str(i)})
               for i,c in enumerate(s.candidates)]
    groups = list(planner.groups(p.evidence(s), options))
    assert len(groups) > 1
    assert [o["description"] for item,_ in groups for o in item["options"]] == [o["description"] for o in options]
    assert all(audit["fits"] for _,audit in groups)


def test_invalid_choices_and_deadline_never_produce_a_click(planner):
    backend = Backend()
    backend._post = lambda payload: {"answers": {"next": {"choice": "NOT_OFFERED"}}}
    brain = UnifiedBrain("jev", backend=backend, planner=planner)
    with pytest.raises(ValueError, match="invalid_unified_choice"):
        brain.choose_action(state(2))
    brain = UnifiedBrain("jev", backend=Backend(), planner=planner)
    original = brain.infer
    def expire(item, audit):
        result = original(item, audit)
        brain.set_deadline(time.perf_counter()-1)
        return result
    brain.infer = expire
    with pytest.raises(TimeoutError) as exc:
        brain.choose_action(state(601))
    assert len(exc.value.brain_debug["unified_calls"]) == 1
    assert _api_usage(exc.value.brain_debug)["requests"] == 1


def test_http_failure_preserves_exact_failed_input_and_response_body(planner):
    import httpx
    backend = Backend()
    def fail(payload):
        response = httpx.Response(400, text='{"error":"test rejection"}',
                                  request=httpx.Request("POST", "https://example.test/choice"))
        response.raise_for_status()
    backend._post = fail
    brain = UnifiedBrain("jev", backend=backend, planner=planner)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        brain.choose_action(state(255))
    failed = exc.value.brain_debug["unified_failed_request"]
    assert failed["status_code"] == 400
    assert "test rejection" in failed["response_body"]
    assert len(failed["canonical"]["options"]) == 255
    assert failed["request"]["questions"] == p.choice_questions(failed["canonical"])


def test_laya_complete_preparation_matches_native_layout_and_retains_long_option(planner):
    pytest.importorskip("laya_mlx")
    from laya_mlx.agent import Agent
    from laya_mlx.tokenizer import Tokenizer
    from wikirace.laya_mlx_brain import strict_prepare
    native = Agent.__new__(Agent)
    native.tok = Tokenizer(p.ROOT / "models/laya-mlx/tokenizer")
    native.cfg = {"max_len":8192, "head_max_len":7800}
    native._prefix_cache = None
    s = state(255)
    shared = p.evidence(s)
    item = p.decision(shared, [p.option(c) for c in s.candidates])
    questions = p.choice_questions(item)
    before = native.prepare(shared, questions)
    backend = Backend()
    backend._agent_or_load = lambda: native
    brain = UnifiedBrain("laya-mlx", backend=backend, planner=planner)
    brain.load()
    assert native.prepare(shared, questions) == before
    item["options"][0]["description"] = "Long distinctive article title " * 50
    questions = p.choice_questions(item)
    prepared, _ = native.prepare(shared, questions)
    assert len(prepared[0]["markers"]) == 255
    strict_prepare(native, shared, questions)


def test_semif_full_prompt_matches_shared_tokenizer_and_255_unique_answer_slots(planner):
    pytest.importorskip("transformers")
    from transformers import AutoTokenizer
    from semif_phase1 import core, direct
    tokenizer = AutoTokenizer.from_pretrained(p.ROOT / "models/Qwen3.5-4B", local_files_only=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core, "LETTERS", planner.spec["codes"])
        patch.setattr(direct, "LETTERS", planner.spec["codes"])
        patch.setattr(core, "DIRECT_SYSTEM", planner.spec["system"])
        s = state()
        item = p.decision(p.evidence(s), [p.option(c) for c in s.candidates])
        ids, slots, _ = direct.encode_prompt(tokenizer, p.semif_row(item), p.MAX_TOKENS)
        assert len(slots) == len(set(slots)) == 255
        assert p.digest(ids) == planner.audit(item)["semif_ids_sha256"]


def test_semif_unicode_input_matches_runtime_tokenizer(planner):
    pytest.importorskip("transformers")
    from transformers import AutoTokenizer
    from semif_phase1 import core, direct
    tokenizer = AutoTokenizer.from_pretrained(p.ROOT / "models/Qwen3.5-4B", local_files_only=True)
    s = state()
    s.current.description = "Manipur (/ˌmænɪˈpʊər/, IPA: [məɳiˈpʊɾ])"
    s.candidates[100].context = "The name Manipur (Sanskrit: मणिपुर, romanized: m"
    s.candidates[180].title = "日本語 中文 العربية தமிழ் Ελληνικά"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core, "LETTERS", planner.spec["codes"])
        patch.setattr(direct, "LETTERS", planner.spec["codes"])
        patch.setattr(core, "DIRECT_SYSTEM", planner.spec["system"])
        for item, audit in planner.groups(p.evidence(s), [p.option(c) for c in s.candidates]):
            ids, slots, _ = direct.encode_prompt(tokenizer, p.semif_row(item), p.MAX_TOKENS)
            assert p.digest(ids) == audit["semif_ids_sha256"]
            assert len(slots) == len(item["options"])
