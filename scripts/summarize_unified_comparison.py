"""Audit actual shared inputs and summarize the independent unified-protocol run."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wikirace import unified_protocol as p
from scripts.summarize_page_retest import checkpoints


def audit_call(call):
    item = call["canonical"]
    assert p.digest({k:v for k,v in item.items() if k != "sha256"}) == item["sha256"]
    assert item["instruction"] == p.INSTRUCTION and item["version"] == p.VERSION
    assert 2 <= len(item["options"]) <= 255
    assert call["winner"] in [o["id"] for o in item["options"]]
    audit = call["input_audit"]
    assert audit["fits"] and audit["markers"] == len(item["options"])
    assert audit["laya_tokens"] <= 8192 and audit["semif_tokens"] <= 8192 and audit["laya_head_tokens"] <= 7800
    request = call["request"]
    assert request["state"] == item["state"]
    if "questions" in request:
        assert request["questions"] == p.choice_questions(item)
    else:
        assert request == p.semif_row(item)


def audit_episode(row):
    count = 0
    for step in row["trace"]:
        debug = step["brain_debug"]
        obs = step["observation"]
        expected_state = p.evidence({"goal":obs["goal"], "current":obs["current"],
                                     "recent_path":obs["action"]["path"][-6:]})
        options = {o["id"]:p.option(o) for o in obs["offered_links"]}
        calls = debug.get("unified_calls", [])
        for call in calls:
            audit_call(call)
            item = call["canonical"]
            assert item["state"] == expected_state
            for option in item["options"]:
                assert {k:v for k,v in option.items() if k != "code"} == options[option["id"]]
        previous = list(options)
        hashes = {c["canonical"]["sha256"]:c for c in calls}
        for groups in debug.get("unified_rounds", []):
            assert [cid for group in groups for cid in group["ids"]] == previous
            for group in groups:
                assert group["winner"] in group["ids"]
                if group["bye"]:
                    assert len(group["ids"]) == 1
                else:
                    assert hashes[group["canonical_sha256"]]["winner"] == group["winner"]
            previous = [g["winner"] for g in groups]
        if step.get("action"):
            assert previous == [step["action"]["link_id"]]
        assert step["api_usage"]["requests"] == len(calls)
        count += len(calls)
    assert row["scrolls"] == 0
    return {"calls":count, "all_saved_calls_audited":True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.root / "manifest.json").read_text())
    result = {"protocol":p.VERSION, "root":str(args.root), "complete":manifest.get("complete",False),
              "fixed_input_parity_verified":manifest.get("fixed_input_parity_verified",False),
              "probes":[], "episodes":[], "pending":[]}
    result["archived_attempts"] = []
    pause_path = args.root / "pause.json"
    if pause_path.exists():
        result["pause"] = json.loads(pause_path.read_text())
    for path in sorted(args.root.glob("race/*/attempts/*/*/*.summary.json")):
        attempt = json.loads(path.read_text())
        result["archived_attempts"].append({"source":str(path), **{k:attempt.get(k) for k in
            ("brain","task_id","status","reason","seconds","clicks","path","code_fingerprint")}})
    hashes = {}
    for name in ("jev", "laya-mlx", "semif"):
        path = args.root / "probe" / name / "probe.json"
        if path.exists():
            probe = json.loads(path.read_text())
            for measurement in probe["measurements"]:
                for call in measurement["calls"]:
                    audit_call(call)
                hashes.setdefault(measurement["task"],set()).add(measurement["canonical_sha256"])
            for control in probe["controls"]:
                audit_call(control["call"])
            for page in probe["full_first_pages"]:
                for call in page["debug"]["unified_calls"]:
                    audit_call(call)
            result["probes"].append({"brain":name, "source":str(path), "complete":probe.get("complete",False),
                "load_seconds":probe["load_seconds"], "code_fingerprint":probe["code_fingerprint"],
                "controls_correct":sum(c["correct"] for c in probe["controls"]), "controls_total":len(probe["controls"]),
                "peak_mlx_allocation_mib":probe.get("peak_mlx_allocation_mib"),
                "measurements":[{k:v for k,v in m.items() if k != "calls"} for m in probe["measurements"]],
                "controls":[{k:v for k,v in c.items() if k != "call"} for c in probe["controls"]],
                "full_first_pages":[{"task":page["task"], "seconds":page["seconds"], "selected":page["selected"],
                    "first_round_groups":[{"count":len(g["ids"]),"sha256":g["canonical_sha256"]}
                                          for g in page["debug"]["unified_rounds"][0]]}
                    for page in probe["full_first_pages"]]})
        for task in ("hard-dna","hard-music","hard-wwii"):
            path = args.root / "race" / name / (task + ".json")
            if not path.exists():
                result["pending"].append({"brain":name,"task":task})
                continue
            row = json.loads(path.read_text())
            fields = ("brain","task_id","status","reason","clicks","seconds","setup_seconds","total_seconds", "path",
                      "code_fingerprint","api_usage","timeout_s","started_at","goal_description","article_filter_version")
            result["episodes"].append({**{k:row.get(k) for k in fields}, "source":str(path),
                "audit":audit_episode(row), "time_checkpoints":checkpoints(row),
                "resolved_models":sorted({str(call["response"].get("model")) for s in row["trace"]
                                           for call in s["brain_debug"].get("unified_calls",[])}),
                "phase_seconds":{k:round(sum(s.get(k,0) or 0 for s in row["trace"])/1000,3)
                                 for k in ("score_ms","choice_ms","execution_ms","observe_ms")}})
    assert all(len(h) == 1 for h in hashes.values()), "fixed inputs differ across backends"
    probe_fingerprints = {r["code_fingerprint"] for r in result["probes"]}
    race_fingerprints = {e["code_fingerprint"] for e in result["episodes"]}
    assert len(probe_fingerprints) <= 1, "code changed between fixed-input probes"
    if manifest.get("runtime_fix_audit"):
        from scripts.unified_runtime_audit import validate_runtime_audit
        # Reports audit saved historical runs, not today's checkout. Resume-time
        # validation still requires the current runtime fingerprint to match.
        proof_path = ROOT / manifest["runtime_fix_audit"]
        recorded_fingerprint = json.loads(proof_path.read_text())["after_fingerprint"]
        proof = validate_runtime_audit(proof_path, recorded_fingerprint)
        assert race_fingerprints <= {proof["before_fingerprint"], proof["after_fingerprint"]}
        result["runtime_fix_audit"] = manifest["runtime_fix_audit"]
    else:
        assert len(race_fingerprints) <= 1, "code changed between live races"
    result["phase_code_fingerprints"] = {"probe":sorted(probe_fingerprints), "race":sorted(race_fingerprints)}
    result["cross_phase_code_fingerprint_match"] = probe_fingerprints == race_fingerprints
    result["reused_probes_from"] = manifest.get("reused_probes_from")
    result["reused_jev_race_from"] = manifest.get("reused_jev_race_from")
    if result["complete"]:
        assert len(result["episodes"]) == 9 and not result["pending"]
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"complete":result["complete"], "episodes":len(result["episodes"]),
                      "pending":len(result["pending"]), "controls":{r["brain"]:r["controls_correct"] for r in result["probes"]}},ensure_ascii=False))


if __name__ == "__main__":
    main()
