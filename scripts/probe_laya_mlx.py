"""Offline MLX smoke benchmark and optional audit of a saved WikiRace request.

Run with .venv-mlx/bin/python; this does not launch Chrome or call a paid API.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import laya_mlx
import mlx.core as mx
from laya_mlx.common import build_prefix, serialize_state

ROOT = Path(__file__).resolve().parents[1]
REVISION = "047678560251f28113ee8f5df4be82102c7bf336"
WEIGHT_SHA256 = "b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de"


def measure(agent, state, questions, iterations):
    agent.predict(state, questions)  # warmup excluded from samples
    mx.synchronize()
    samples, result = [], None
    for _ in range(iterations):
        start = time.perf_counter()
        result = agent.predict(state, questions)
        mx.synchronize()
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "iterations": iterations,
        "warmup_calls": 1,
        "median_ms": round(statistics.median(samples), 3),
        "samples_ms": [round(x, 3) for x in samples],
        "questions": len(questions),
        "answers": result["answers"],
    }


def audit_request(agent, request):
    state, questions = request["state"], request["questions"]
    items, internal = agent.prepare(state, questions)
    original_tokens = len(agent.tok(serialize_state(state))["input_ids"])
    rows = []
    for (qid, definition), item, question in zip(questions.items(), items, internal):
        prefix, _ = build_prefix(agent.tok, question, agent.cfg["head_max_len"])
        state_ids = item["ids"][len(prefix):-1]
        visible_state = agent.tok.backend.decode(state_ids)
        row = {
            "qid": qid,
            "total_tokens": len(item["ids"]),
            "original_state_tokens": original_tokens,
            "retained_state_tokens": len(state_ids),
            "visible_state": visible_state,
        }
        if question["t"] == "score":
            candidates = state.get("candidates", {})
            candidate_id = definition["instructions"].get("candidate_id")
            row.update(
                target_candidate_id=candidate_id,
                target_candidate_id_visible=candidate_id in visible_state,
                visible_candidate_ids=[k for k in candidates if f'"{k}"' in visible_state],
            )
        elif question["t"] == "choice":
            # Decode each option separately: a title appearing in the shared state
            # is not evidence that the option's own title survived truncation.
            options = []
            marks = item["markers"]
            for i, (key, value) in enumerate(definition["criteria"].items()):
                end = marks[i + 1] if i + 1 < len(marks) else len(prefix) - 1
                rendered = agent.tok.backend.decode(item["ids"][marks[i]:end])
                title = value.get("title", "") if isinstance(value, dict) else str(value)
                options.append({"id": key, "title": title, "visible_text": rendered,
                                "full_title_visible": bool(title) and title in rendered})
            row["options"] = options
            row["full_option_titles_visible"] = sum(x["full_title_visible"] for x in options)
            row["visible_instruction"] = agent.tok.backend.decode(item["ids"][:marks[0]])
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=ROOT / "models/laya-mlx")
    parser.add_argument("--trace", type=Path, help="Saved episode; audit its first Score batch and Choice")
    parser.add_argument("--out", type=Path, default=ROOT / "runs/laya-mlx-probe.json")
    args = parser.parse_args()
    model = args.model.resolve(strict=True)
    if not mx.metal.is_available():
        raise RuntimeError("This probe requires Apple Silicon with Metal available")
    with (model / "model.safetensors").open("rb") as f:
        checksum = hashlib.file_digest(f, "sha256").hexdigest()
    if checksum != WEIGHT_SHA256:
        raise ValueError("Weights do not match the pinned English Laya checkpoint")
    mx.set_cache_limit(512 * 1024 * 1024)
    start = time.perf_counter()
    agent = laya_mlx.load(str(model), device="gpu", dtype="float16", batch_size=16)
    mx.synchronize()
    load_seconds = time.perf_counter() - start
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
        "device": mx.device_info(),
        "packages": {p: importlib.metadata.version(p) for p in
                     ("laya-mlx", "mlx", "mlx-metal", "tokenizers", "huggingface-hub")},
        "model": "aac6fef/laya-mlx", "revision": REVISION,
        "weight_sha256": checksum, "local_path": str(model),
        "dtype": "float16", "batch_size": 16,
        "compile": False, "cache_prompts": False,
        "allocation_cache_limit_mib": 512,
        "load_seconds": round(load_seconds, 3),
        "timing_scope": "Synchronized predict, including preparation and result formatting; excludes model load and one warmup per case",
    }
    cases = {
        "short_choice": ("I was billed twice. Please refund the duplicate.", {
            "department": {"type": "choice", "instructions": "Who should handle this?",
                           "criteria": ["billing", "technical", "sales"]}}),
        "short_score": ("I was billed twice. Please refund the duplicate.", {
            "refund": {"type": "score", "instructions": "How clearly is a refund requested?",
                       "criteria": ["No request", "Unclear request", "Explicit refund request"]}}),
        "compact_wikirace_choice": (
            "Current: Coffee. Goal: Caffeine, the stimulant naturally present in coffee.", {
                "next": {"type": "choice", "instructions": "Choose the link to the goal.",
                         "criteria": {"L001": "Tea", "L002": "Caffeine", "L003": "Agriculture"}}}),
    }
    report["smoke"] = {name: measure(agent, state, questions, 10)
                       for name, (state, questions) in cases.items()}
    if args.trace:
        trace = json.loads(args.trace.read_text(encoding="utf-8"))
        debug = trace["trace"][0]["brain_debug"]
        requests = {"score_batch": debug["score_batches"][0]["score_request"],
                    "choice": debug["request"]}
        report["replay"] = {
            "source": str(args.trace), "source_run_id": trace["run_id"],
            "note": "Replay of saved Jev payload through Laya MLX; not a reproduction of the unavailable CPU Laya episode",
            "requests": {},
        }
        for name, request in requests.items():
            print(f"Measuring saved {name}: {len(request['questions'])} questions", flush=True)
            report["replay"]["requests"][name] = {
                "audit": audit_request(agent, request),
                "timing": measure(agent, request["state"], request["questions"], 3),
            }
    report["peak_mlx_allocation_mib"] = round(mx.get_peak_memory() / 1024**2, 2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.out), "load_seconds": report["load_seconds"],
                      "smoke_medians_ms": {k: v["median_ms"] for k, v in report["smoke"].items()},
                      "peak_mlx_allocation_mib": report["peak_mlx_allocation_mib"]}, indent=2))


if __name__ == "__main__":
    main()
