"""Real tokenizer coverage tests; no GPU model weights or inference required."""
from pathlib import Path
import time

import pytest

pytest.importorskip("laya_mlx")
from laya_mlx.agent import Agent
from laya_mlx.tokenizer import Tokenizer
from wikirace.laya_mlx_brain import InputBudgetError, LayaMLXBrain, strict_prepare

TOKENIZER = Path(__file__).resolve().parents[1] / "models/laya-mlx/tokenizer"
pytestmark = pytest.mark.skipif(not TOKENIZER.exists(), reason="local English Laya tokenizer needed")


@pytest.fixture
def brain():
    agent = Agent.__new__(Agent)
    agent.tok = Tokenizer(TOKENIZER)
    agent.cfg = {"max_len": 512, "head_max_len": 192}
    agent._prefix_cache = None

    def predict(state, questions):
        strict_prepare(agent, state, questions)
        answers = {}
        for qid, q in questions.items():
            if q["type"] == "choice":
                # A target placed last must survive every round; no adapter shortcut.
                choice = next((k for k, v in state["links"].items() if v["title"] == "Goal Article"),
                              next(reversed(state["links"])))
                answers[qid] = {"type": "choice", "choice": choice}
            else:
                answers[qid] = {"type": "score", "score": 3.5}
        return {"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}}

    agent.predict = predict
    return LayaMLXBrain(agent=agent)


def state():
    return {"goal": {"title": "Goal Article", "description": "A specific place in Michigan."},
            "current": {"title": "Start"}, "recent_path": ["Start"]}


@pytest.mark.parametrize("count", [1, 2, 64, 255])
def test_choice_covers_every_offered_title_and_keeps_target_at_end(brain, count):
    criteria = {f"L{i:03}": {"title": "Goal Article" if i == count else f"Article {i}",
                              "context": "Some useful information about this article."}
                for i in range(1, count + 1)}
    response = brain._post({"state": state(), "questions": {
        "next": {"type": "choice", "criteria": criteria}}})
    assert response["answers"]["next"]["choice"] == f"L{count:03}"
    if count == 1:
        assert not response["model_calls"]
        return
    covered = [cid for group in response["choice_rounds"][0] for cid in group["offered_ids"]]
    assert covered == list(criteria)
    for call in response["model_calls"]:
        assert all(a["truncated_tokens"] == 0 for a in call["input_audit"])
        actual_titles = [v["title"] for v in call["request"]["state"]["links"].values()]
        assert actual_titles == [criteria[cid]["title"] for cid in call["candidate_ids"]]


def test_score_has_its_own_candidate_at_end_of_64_batch(brain):
    candidates = {f"L{i:03}": {"title": f"Distinct candidate number {i}", "context": "long " * 500}
                  for i in range(64)}
    questions = {f"score_{cid}": {"type": "score", "instructions": {"candidate_id": cid}}
                 for cid in candidates}
    response = brain._post({"state": {**state(), "candidates": candidates}, "questions": questions})
    assert set(response["answers"]) == set(questions)
    seen = []
    for call in response["model_calls"]:
        for cid, q in zip(call["candidate_ids"], call["request"]["questions"].values()):
            assert candidates[cid]["title"] in q["instructions"]
            seen.append(cid)
    assert seen == list(candidates)


def test_all_link_policy_accepts_more_than_255_candidates(brain):
    brain.page_selection_policy = "grouped-all"
    test_choice_covers_every_offered_title_and_keeps_target_at_end(brain, 601)


def test_strict_guard_rejects_upstream_silent_truncation(brain):
    with pytest.raises(InputBudgetError):
        strict_prepare(brain._agent, "context " * 2000, {
            "q": {"type": "score", "instructions": "Rate usefulness", "criteria": ["bad", "good"]}})


def test_deadline_between_inference_batches_preserves_completed_evidence(brain):
    original = brain._agent.predict

    def expire(state, questions):
        result = original(state, questions)
        brain.set_deadline(time.perf_counter() - 1)
        return result

    brain._agent.predict = expire
    criteria = {f"L{i}": {"title": f"Title {i}"} for i in range(20)}
    with pytest.raises(TimeoutError) as error:
        brain._post({"state": state(), "questions": {"next": {"type": "choice", "criteria": criteria}}})
    assert len(error.value.brain_debug["mlx_partial_calls"]) == 1
