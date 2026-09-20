"""Page-level Jev routing: bounded scoring batches, then one click Choice.

This follows the public two-stage architecture, not an unpublished demo prompt.
"""
from wikirace.env import PAGE_CHOICE_LIMIT
from wikirace.state import Action, LinkScore

PAGE_SCORE_BATCH = 64
PROMPT_VERSION = "page-target-v1"
LEVELS = [
    "Unrelated to the specific target, or points to a conflicting location/entity.",
    "Only a broad shared topic or word; little evidence of a useful route.",
    "Plausible intermediate hub for the target's region or domain, but indirect.",
    "Specific bridge through the correct location, organization, event series or relevant list.",
    "The exact target, or a dedicated index/closely connected article likely to link directly to it.",
]
FOCUS = (
    "Reach the specific goal article in as few link clicks as possible. "
    "Use its description to preserve the identity of the target: its location, named "
    "organization, event series or other distinguishing facts. A shared broad topic "
    "or word alone is weak evidence. Prefer specific relevant lists and concrete "
    "entity connections. A broader hub can be useful when it offers a credible route; "
    "literal title similarity is not required. Do not invent an intermediate path. "
    "All options are actual rendered links on the current article. "
    "Previously visited destinations have already been filtered by code."
)


def _state(state):
    return {
        "task": "wikirace_article_links",
        "goal": {"title": state.goal.title, "description": state.goal.description},
        "current": {"title": state.current.title, "description": state.current.description},
        "recent_path": (state.history or state.action.path)[-6:],
    }


def _candidate(candidate):
    result = {"title": candidate.title, "url": candidate.url,
              "context": (candidate.context or candidate.text)[:160]}
    if candidate.score is not None:
        result["bridge_score"] = candidate.score
    return result


def score_page(brain, state):
    candidates = state.candidates
    if len(candidates) <= PAGE_CHOICE_LIMIT:
        return [], {"page_policy": "direct_choice", "n_page_links": len(state.page_links),
                    "n_eligible_links": len(candidates), "prompt_version": PROMPT_VERSION}
    scored, batches = [], []
    for start in range(0, len(candidates), PAGE_SCORE_BATCH):
        batch = candidates[start:start + PAGE_SCORE_BATCH]
        payload = {"model": brain.model, "state": {
            **_state(state), "candidates": {c.id: _candidate(c) for c in batch},
        }, "questions": {
            f"score_{c.id}": {
                "type": "score", "instructions": {
                    "question": f"How useful is candidate {c.id} as the next link toward this specific goal?",
                    "focus": FOCUS, "candidate_id": c.id,
                }, "criteria": LEVELS,
            } for c in batch
        }}
        try:
            response = brain._post(payload)
        except Exception as exc:
            # Retain completed batches if a later batch hits the deadline.
            exc.brain_debug = {"score_batches": batches, "n_scored": len(scored),
                               "prompt_version": PROMPT_VERSION, "partial_scoring": True}
            raise
        batches.append({"score_request": payload, "score_response": response})
        for c in batch:
            answer = response.get("answers", {}).get(f"score_{c.id}", {})
            if "score" not in answer:
                raise ValueError(f"missing_page_score:{c.id}")
            value = float(answer["score"])
            if not 0 <= value <= len(LEVELS) - 1:
                raise ValueError(f"invalid_page_score:{c.id}")
            scored.append(LinkScore(id=c.id, title=c.title, context=c.context,
                                    score=round(value / (len(LEVELS) - 1), 4)))
    return scored, {"page_policy": "score_then_choice", "score_batches": batches,
                    "n_page_links": len(state.page_links), "n_eligible_links": len(candidates),
                    "n_scored": len(scored), "n_batches": len(batches), "prompt_version": PROMPT_VERSION}


def choose_page(brain, state):
    if not state.candidates:
        return Action(action="click", link_id=""), {"fail_reason": "page_links_empty"}
    if len(state.candidates) > PAGE_CHOICE_LIMIT:
        raise ValueError("page_shortlist_not_refreshed")
    payload = {"model": brain.model, "state": _state(state), "questions": {
        "next": {"type": "choice", "instructions": {
            "question": "Which single article link is the best next step toward the specific goal?",
            "focus": FOCUS + " Choose the exact goal if it is offered. "
                     "Bridge scores are ranking hints, not probabilities of success.",
        }, "criteria": {c.id: _candidate(c) for c in state.candidates}}
    }}
    response = brain._post(payload)
    choice = response.get("answers", {}).get("next", {}).get("choice")
    if choice not in payload["questions"]["next"]["criteria"]:
        raise ValueError(f"invalid_page_choice:{choice}")
    return Action(action="click", link_id=choice), {
        "request": payload, "response": response, "prompt_version": PROMPT_VERSION,
        "choice_candidate_count": len(state.candidates), "finalist_mode": False,
    }
