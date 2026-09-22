import time

import pytest

from wikirace.semif_brain import SemIfBrain


class FakeScorer:
    def score(self, row):
        ids = [option["id"] for option in row["options"]]
        winner = next((i for i, option in enumerate(row["options"])
                       if option["description"].startswith("Goal article")), len(ids) - 1)
        return {"option_ids": ids, "probabilities": [float(i == winner) for i in range(len(ids))],
                "input_tokens": 200, "model": {"test": True}}


@pytest.fixture
def brain(monkeypatch):
    b = SemIfBrain(scorer=FakeScorer())
    def validate(row):
        assert 2 <= len(row["options"]) <= 16
        return 200
    monkeypatch.setattr(b, "_validate_input", validate)
    return b


def payload(count):
    return {"state": {"goal": {"title": "Goal article"}}, "questions": {
        "next": {"type": "choice", "criteria": {
            f"L{i}": {"title": "Goal article" if i == count - 1 else f"Article {i}",
                      "context": "A real article context."} for i in range(count)}}}}


@pytest.mark.parametrize("count", [1, 2, 16, 17, 64, 255, 1041])
def test_all_candidates_participate_and_winners_are_recompared(brain, count):
    request = payload(count)
    result = brain._post(request)
    assert result["answers"]["next"]["choice"] == f"L{count - 1}"
    previous = list(request["questions"]["next"]["criteria"])
    for groups in result["choice_rounds"]:
        assert [cid for group in groups for cid in group["offered_ids"]] == previous
        assert all(group["winner"] in group["offered_ids"] for group in groups)
        previous = [group["winner"] for group in groups]
    assert len(previous) == 1
    for call in result["model_calls"]:
        assert call["candidate_ids"] == [o["id"] for o in call["request"]["options"]]
        for option in call["request"]["options"]:
            assert request["questions"]["next"]["criteria"][option["id"]]["title"] in option["description"]


def test_input_overflow_shrinks_group_without_dropping_options(brain, monkeypatch):
    def small_budget(row):
        if len(row["options"]) > 4:
            raise ValueError("input tokens exceed limit 4096; no truncation allowed")
    monkeypatch.setattr(brain, "_validate_input", small_budget)
    result = brain._post(payload(31))
    assert result["answers"]["next"]["choice"] == "L30"
    assert [c for g in result["choice_rounds"][0] for c in g["offered_ids"]] == [f"L{i}" for i in range(31)]
    assert all(len(c["candidate_ids"]) <= 4 for c in result["model_calls"])


def test_deadline_preserves_completed_native_calls(brain):
    original = brain._scorer.score
    def expire(row):
        result = original(row)
        brain.set_deadline(time.perf_counter() - 1)
        return result
    brain._scorer.score = expire
    with pytest.raises(TimeoutError) as error:
        brain._post(payload(64))
    assert len(error.value.brain_debug["mlx_partial_calls"]) == 1


def test_unexpected_option_mapping_is_rejected(brain):
    brain._scorer.score = lambda row: {"option_ids": ["invented", "ids"], "probabilities": [.5, .5]}
    with pytest.raises(ValueError, match="invalid_semif_option_scores"):
        brain._post(payload(2))
