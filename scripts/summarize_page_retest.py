"""Summarize saved episodes and verify actual MLX candidate coverage."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wikirace.browser import ARTICLE_FILTER_VERSION, title_from_wiki_url


def summarize(path):
    row = json.loads(path.read_text())
    local = row["brain"] in {"laya-mlx", "semif"}
    token_limit = 4096 if row["brain"] == "semif" else 512
    calls, audit_rows, score_count, choice_count = 0, 0, 0, 0
    partial_calls, partial_audits, max_input_tokens, model_ms = 0, 0, 0, 0
    versions, resolved_models = set(), set()
    invalid_urls = []
    for step in row["trace"]:
        debug = step["brain_debug"]
        responses = []
        for batch in debug.get("score_batches", []):
            response = batch["score_response"]
            responses.append(response)
            if local:
                expected = list(batch["score_request"]["state"]["candidates"])
                actual = [cid for call in response["model_calls"] for cid in call["candidate_ids"]]
                assert actual == expected, (path, step["step"], "score coverage")
                for call in response["model_calls"]:
                    definitions = list(call["request"]["questions"].values())
                    assert len(definitions) == len(call["candidate_ids"])
                    for cid, definition in zip(call["candidate_ids"], definitions):
                        title = batch["score_request"]["state"]["candidates"][cid]["title"]
                        assert title in definition["instructions"], (path, "missing score title", cid)
                score_count += len(actual)
        if debug.get("response"):
            response = debug["response"]
            responses.append(response)
            if local:
                expected = list(debug["request"]["questions"]["next"]["criteria"])
                rounds = response["choice_rounds"]
                if rounds:
                    actual = [cid for group in rounds[0] for cid in group["offered_ids"]]
                    assert actual == expected, (path, step["step"], "choice coverage")
                    previous = expected
                    for groups in rounds:
                        assert [c for g in groups for c in g["offered_ids"]] == previous
                        assert all(g["winner"] in g["offered_ids"] for g in groups)
                        previous = [g["winner"] for g in groups]
                    assert previous == [response["answers"]["next"]["choice"]]
                    submitted = [g["offered_ids"] for groups in rounds for g in groups if not g.get("bye")]
                    assert [c["candidate_ids"] for c in response["model_calls"]] == submitted
                    criteria = debug["request"]["questions"]["next"]["criteria"]
                    for call in response["model_calls"]:
                        if row["brain"] == "semif":
                            options = call["request"]["options"]
                            assert [o["id"] for o in options] == call["candidate_ids"]
                            assert len(options) <= 16
                            for option in options:
                                assert option["description"].startswith(criteria[option["id"]]["title"])
                        else:
                            titles = [value["title"] for value in call["request"]["state"]["links"].values()]
                            assert titles == [criteria[cid]["title"] for cid in call["candidate_ids"]]
                else:
                    assert len(expected) == 1
                choice_count += len(expected)
        for response in responses:
            versions.add(response.get("runtime", {}).get("adapter_version", "remote"))
            if response.get("model"):
                resolved_models.add(response["model"])
            for call in response.get("model_calls", []):
                calls += 1
                model_ms += call["milliseconds"]
                for audit in call["input_audit"]:
                    assert audit["truncated_tokens"] == 0
                    assert audit["input_tokens"] <= token_limit
                    max_input_tokens = max(max_input_tokens, audit["input_tokens"])
                    audit_rows += 1
        # The final Score/Choice phase may hit its deadline after some native
        # calls completed. Audit these too, without claiming full phase coverage.
        for call in debug.get("mlx_partial_calls", []):
            partial_calls += 1
            model_ms += call["milliseconds"]
            for audit in call["input_audit"]:
                assert audit["truncated_tokens"] == 0
                assert audit["input_tokens"] <= token_limit
                max_input_tokens = max(max_input_tokens, audit["input_tokens"])
                partial_audits += 1
        if step.get("action"):
            expected = debug.get("request", {}).get("questions", {}).get("next", {}).get("criteria", {})
            assert step["action"]["link_id"] in expected, (path, "unoffered click")
        if row.get("page_selection_policy") == "grouped-all" and debug.get("response"):
            assert len(debug["request"]["questions"]["next"]["criteria"]) == step["n_eligible_links"]
        candidates = list(debug.get("request", {}).get("questions", {}).get("next", {}).get("criteria", {}).values())
        candidates += [c for batch in debug.get("score_batches", [])
                       for c in batch["score_request"]["state"]["candidates"].values()]
        invalid_urls.extend(c.get("url") for c in candidates if not title_from_wiki_url(c.get("url") or ""))
    if row.get("article_filter_version") == ARTICLE_FILTER_VERSION:
        assert not invalid_urls, (path, "invalid article URLs offered", invalid_urls[:3])
    fields = ["brain", "model", "run_id", "task_id", "start", "goal", "status", "reason", "clicks",
              "scrolls", "seconds", "setup_seconds", "total_seconds", "path", "api_usage", "code_fingerprint",
              "observation_mode", "started_at", "timeout_s", "page_selection_policy", "article_filter_version"]
    return {**{key: row.get(key) for key in fields}, "source": str(path),
            "time_checkpoints": checkpoints(row),
            "invalid_candidate_url_occurrences": len(invalid_urls),
            "resolved_models": sorted(resolved_models),
            "phase_seconds": {key: round(sum(step.get(key, 0) or 0 for step in row["trace"]) / 1000, 3)
                              for key in ["score_ms", "choice_ms", "execution_ms", "observe_ms"]},
            "max_choice_candidates": max((s.get("n_choice_candidates", 0) for s in row["trace"]), default=0),
            "mlx_audit": {"model_calls": calls, "fully_retained_questions": audit_rows,
                          "partial_phase_model_calls": partial_calls,
                          "partial_phase_retained_questions": partial_audits,
                          "all_model_calls": calls + partial_calls,
                          "all_retained_questions": audit_rows + partial_audits,
                          "max_input_tokens": max_input_tokens,
                          "native_predict_seconds": round(model_ms / 1000, 3),
                          "completed_score_candidates": score_count, "completed_choice_candidates": choice_count,
                          "versions": sorted(versions), "completed_call_coverage_verified": local}}


def checkpoints(row):
    """Same continuous trajectory at fixed budgets; no restart at 30 minutes."""
    result = []
    if not any("elapsed_seconds" in s for s in row["trace"]):
        return result  # historical records do not have exact per-action timestamps
    finished = row["seconds"]
    if row["status"] == "success":
        completed = [s["action_elapsed_seconds"] for s in row["trace"] if "action_elapsed_seconds" in s]
        if completed:
            finished = completed[-1]
    for limit in [600, 1800, 3600]:
        ended = finished <= limit or (
            row["reason"] == "timeout" and row.get("timeout_s", float("inf")) <= limit)
        actions = [s for s in row["trace"] if s.get("action_elapsed_seconds", float("inf")) <= limit]
        last = actions[-1] if actions else {}
        result.append({"budget_seconds": limit,
                       "status": row["status"] if ended else "not_reached_yet",
                       "reason": row["reason"] if ended else "budget_checkpoint",
                       "clicks": row["clicks"] if ended else last.get("clicks_so_far", 0),
                       "last_action_seconds": last.get("action_elapsed_seconds"),
                       "last_clicked_title": last.get("chosen_title")})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    rows = [summarize(path) for directory in args.directories for path in sorted(directory.glob("*.json"))
            if not path.name.endswith(".summary.json")]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"runs": rows, "note": "One run per condition; no stable success-rate estimate."},
                                    ensure_ascii=False, indent=2) + "\n")
    for r in rows:
        print(r["brain"], r["task_id"], r["status"], r["clicks"], r["seconds"], r["mlx_audit"])


if __name__ == "__main__":
    main()
