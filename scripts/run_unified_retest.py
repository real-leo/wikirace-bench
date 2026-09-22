"""Matched-input probes or live three-task runs, one backend per process."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from wikirace import unified_protocol as protocol
from wikirace.unified_brain import UnifiedBrain
from wikirace.eval import run_episode, _code_fingerprint
from wikirace.state import Candidate, PageRef, RaceState

FIXTURES = ROOT / "reports/2026-09-21-unified-fixtures.json"


def external_error(row):
    """Transport/navigation failures don't invalidate other backends' inputs."""
    reason = row.get("reason", "")
    return row.get("status") == "error" and (
        "HTTPStatusError" in reason or "PageLoadError" in reason or
        any(name in reason for name in ("ConnectError", "ReadTimeout", "ConnectTimeout")))


def prepare_resume(out_dir, tasks):
    """Keep completed episodes; archive failed/partial attempts before retrying."""
    pending = []
    for task in tasks:
        path = out_dir / (task["id"] + ".json")
        if path.exists() and json.loads(path.read_text())["status"] != "error":
            print("REUSE", task["id"], flush=True)
            continue
        files = [out_dir / (task["id"] + suffix) for suffix in
                 (".json", ".summary.json", ".progress.jsonl", ".json.tmp", ".summary.json.tmp")]
        existing = [file for file in files if file.exists()]
        if existing:
            parent = out_dir / "attempts" / task["id"]
            number = 1
            while (parent / str(number)).exists():
                number += 1
            archive = parent / str(number)
            archive.mkdir(parents=True)
            for file in existing:
                shutil.move(str(file), str(archive / file.name))
            print("ARCHIVE", task["id"], str(archive), flush=True)
        pending.append(task)
    return pending


def save(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    # Escape Unicode in diagnostic JSON too: even malformed browser strings
    # must survive an error path and remain available for diagnosis.
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def probe(brain, out, metadata):
    cases = json.loads(FIXTURES.read_text())["cases"]
    result = {**metadata, "fixture_sha256": hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
              "measurements": [], "controls": [], "full_first_pages": []}
    first = cases[0]
    shared = protocol.evidence(first["state"])
    options = [protocol.option({"id": cid, **v}) for cid, v in list(first["candidates"].items())[:255]]
    # Discard both a small and full-size warmup before measuring.
    for count in (4, 255):
        item = protocol.decision(shared, options[:count])
        brain.infer(item, brain.planner.audit(item))
    for case in cases:
        shared = protocol.evidence(case["state"])
        options = [protocol.option({"id": cid, **v}) for cid, v in list(case["candidates"].items())[:255]]
        item = protocol.decision(shared, options)
        audit = brain.planner.audit(item)
        assert len(options) == 255 and audit["fits"]
        calls, seconds = [], []
        for _ in range(3):
            started = time.perf_counter()
            winner, call = brain.infer(item, audit)
            seconds.append(round(time.perf_counter() - started, 6))
            calls.append(call)
        row = {"task": case["task"], "seconds": seconds, "median_seconds": statistics.median(seconds),
               "canonical_sha256": item["sha256"], "audit": audit,
               "selected_titles": [next(o["title"] for o in options if o["id"] == c["winner"]) for c in calls],
               "calls": calls}
        result["measurements"].append(row)
        print("PROBE", case["task"], row["median_seconds"], row["selected_titles"], flush=True)
        save(out, result)
        for position in (0, 127, 254):
            control = list(options)
            goal_id = control[position]["id"]  # keep neutral ID; no answer-revealing marker
            control[position] = protocol.option({"id": goal_id, "title": case["state"]["goal"]["title"]})
            item = protocol.decision(shared, control)
            winner, call = brain.infer(item, brain.planner.audit(item))
            result["controls"].append({"task": case["task"], "position": position,
                                        "correct": winner == goal_id, "call": call})
            print("CONTROL", case["task"], position, winner == goal_id, flush=True)
            save(out, result)
        state = RaceState(goal=PageRef(**case["state"]["goal"]), current=PageRef(**case["state"]["current"]),
                          history=case["state"]["recent_path"], observation_mode="page",
                          candidates=[Candidate(id=cid, **{k:v for k,v in value.items() if k in Candidate.model_fields})
                                      for cid,value in case["candidates"].items()])
        started = time.perf_counter()
        action, debug = brain.choose_action(state)
        result["full_first_pages"].append({"task": case["task"], "seconds": time.perf_counter()-started,
                                            "selected": state.title_for(action.link_id), "debug": debug})
        save(out, result)
    result["complete"] = True
    if brain.name in {"laya-mlx", "semif"}:
        import mlx.core as mx
        result["peak_mlx_allocation_mib"] = mx.get_peak_memory()/1024**2
    save(out, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain", required=True, choices=["jev", "laya-mlx", "semif", "gemini"])
    parser.add_argument("--phase", required=True, choices=["probe", "race"])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--runtime-fix-audit", type=Path)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=args.resume)
    tasks = json.loads((ROOT / "data/tasks_hard.json").read_text())
    if args.resume:
        old = json.loads((args.out_dir / "metadata.json").read_text())
        if old["code_fingerprint"] != _code_fingerprint():
            if args.runtime_fix_audit is None:
                raise ValueError("cannot_resume_with_changed_runtime_code")
            from scripts.unified_runtime_audit import validate_runtime_audit
            proof = validate_runtime_audit(args.runtime_fix_audit, _code_fingerprint())
            if old["code_fingerprint"] != proof["before_fingerprint"]:
                raise ValueError("runtime_fix_audit_does_not_cover_previous_code")
        if args.phase != "race":
            raise ValueError("only_live_races_can_resume")
        tasks = prepare_resume(args.out_dir, tasks)
        if not tasks:
            return
    load_dotenv(ROOT / ".env")
    if args.brain == "gemini":
        from scripts.unified_chat_brain import UnifiedChatBrain
        brain = UnifiedChatBrain()
    else:
        brain = UnifiedBrain(args.brain)
    start = time.perf_counter()
    brain.load()
    metadata = {"brain": brain.name, "protocol": protocol.VERSION, "load_seconds": time.perf_counter()-start,
                "code_fingerprint": _code_fingerprint(), "instruction": protocol.INSTRUCTION,
                "answer_contract_sha256": hashlib.sha256((ROOT / "data/unified_answer_codes.json").read_bytes()).hexdigest(),
                "qwen_pre_tokenizer_sha256": hashlib.sha256((ROOT / "data/unified_qwen_pre_tokenizer.json").read_bytes()).hexdigest(),
                "input_limits": {"descriptions_chars": protocol.DESCRIPTION_CHARS,
                                 "candidate_context_chars": protocol.CONTEXT_CHARS,
                                 "choices": protocol.MAX_CHOICES, "tokens": protocol.MAX_TOKENS}}
    if args.brain == "gemini":
        metadata.update(model=brain.model, provider_base_url=brain.backend.base_url,
                        chat_adapter_sha256=hashlib.sha256((ROOT / "scripts/unified_chat_brain.py").read_bytes()).hexdigest(),
                        runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        temperature=0, max_output_tokens=2048, output_format="strict_json_schema")
    save(args.out_dir / "metadata.json", metadata)
    print("READY", args.brain, "load_seconds", metadata["load_seconds"], flush=True)
    try:
        if args.phase == "probe":
            probe(brain, args.out_dir / "probe.json", metadata)
            return
        for task in tasks:
            task = {**task, "source": "browser", "observation_mode": "page"}
            with (args.out_dir / (task["id"] + ".progress.jsonl")).open("x") as progress:
                def record(entry, status):
                    row = {"task_id": task["id"], **status,
                           **{k:entry.get(k) for k in ("step", "current", "chosen_title", "clicks_so_far",
                                                      "elapsed_seconds", "action_elapsed_seconds", "error")}}
                    progress.write(json.dumps(row, ensure_ascii=True)+"\n")
                    progress.flush()
                    print("STEP", json.dumps(row, ensure_ascii=True), flush=True)
                row = run_episode(task, brain, timeout_s=args.timeout, progress_callback=record)
            row["unified_protocol"] = metadata
            save(args.out_dir / (task["id"] + ".summary.json"), {k:v for k,v in row.items() if k != "trace"})
            save(args.out_dir / (task["id"] + ".json"), row)
            print("RESULT", task["id"], row["status"], row["reason"], row["clicks"], row["seconds"], flush=True)
            if row["status"] == "error" and not external_error(row):
                raise RuntimeError("unified_episode_error_saved; stopping dispatch: " + row["reason"])
    finally:
        brain.close()


if __name__ == "__main__":
    main()
