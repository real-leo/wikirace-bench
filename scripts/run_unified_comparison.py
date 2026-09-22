"""Sequential matched-input probes, verified parity, then live 60-minute races."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Continue an interrupted root, preserving completed episodes and failed attempts")
    parser.add_argument("--runtime-fix-audit", type=Path)
    parser.add_argument("--reuse-probes", type=Path,
                        help="Reuse a completed probe directory; verify its common-input contract before races")
    parser.add_argument("--reuse-jev-race", type=Path,
                        help="Reuse three completed Jev episodes from this exact runtime fingerprint")
    args = parser.parse_args()
    args.out_root.mkdir(parents=True, exist_ok=args.resume)
    runner = ROOT / "scripts/run_unified_retest.py"
    jobs = [("jev", ".venv-semif"), ("laya-mlx", ".venv-mlx"), ("semif", ".venv-semif")]
    manifest = {"timeout_per_task": args.timeout, "jobs": [],
                "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                "tasks_sha256": hashlib.sha256((ROOT / "data/tasks_hard.json").read_bytes()).hexdigest()}
    if args.resume:
        old = json.loads((args.out_root / "manifest.json").read_text())
        assert old["tasks_sha256"] == manifest["tasks_sha256"]
        assert old["timeout_per_task"] == args.timeout
        old.setdefault("runner_sha256_history", []).append(old["runner_sha256"])
        old["runner_sha256"] = manifest["runner_sha256"]
        old["resumed_at"] = datetime.now(timezone.utc).isoformat()
        old["complete"] = False
        manifest = old
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "USE_TF": "0"}
    if args.runtime_fix_audit:
        from wikirace.eval import _code_fingerprint
        from scripts.unified_runtime_audit import validate_runtime_audit
        validate_runtime_audit(args.runtime_fix_audit, _code_fingerprint())
        manifest["runtime_fix_audit"] = str(args.runtime_fix_audit)
    if args.reuse_probes:
        shutil.copytree(args.reuse_probes, args.out_root / "probe")
        manifest["reused_probes_from"] = str(args.reuse_probes)
    if args.reuse_jev_race:
        from wikirace.eval import _code_fingerprint
        for task in json.loads((ROOT / "data/tasks_hard.json").read_text()):
            row = json.loads((args.reuse_jev_race / (task["id"] + ".json")).read_text())
            assert row["brain"] == "jev" and row["status"] == "success"
            assert row["code_fingerprint"] == _code_fingerprint()
            assert row["timeout_s"] == args.timeout
        shutil.copytree(args.reuse_jev_race, args.out_root / "race" / "jev")
        manifest["reused_jev_race_from"] = str(args.reuse_jev_race)
    def save():
        (args.out_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    phases = ["probe"] if args.probe_only else ["probe", "race"]
    for phase in phases:
        if phase == "probe" and (args.reuse_probes or args.resume):
            continue
        if phase == "race":
            from wikirace import unified_protocol as protocol
            probes = [json.loads((args.out_root / "probe" / name / "probe.json").read_text()) for name,_ in jobs]
            assert all(p["complete"] for p in probes)
            assert all(p["instruction"] == protocol.INSTRUCTION for p in probes)
            contract_hash = hashlib.sha256((ROOT / "data/unified_answer_codes.json").read_bytes()).hexdigest()
            assert all(p["answer_contract_sha256"] == contract_hash for p in probes)
            for field in ("code_fingerprint", "fixture_sha256", "answer_contract_sha256"):
                assert len({p[field] for p in probes}) == 1, field
            for index in range(3):
                assert len({p["measurements"][index]["canonical_sha256"] for p in probes}) == 1
                groups = [[g["canonical_sha256"] for g in p["full_first_pages"][index]["debug"]["unified_rounds"][0]] for p in probes]
                assert groups[0] == groups[1] == groups[2]
            for index in range(9):
                assert len({p["controls"][index]["call"]["canonical"]["sha256"] for p in probes}) == 1
            manifest["fixed_input_parity_verified"] = True
            save()
        phase_jobs = jobs if phase == "probe" else [jobs[0], jobs[2], jobs[1]]
        for brain, venv in phase_jobs:
            if phase == "race" and brain == "jev" and args.reuse_jev_race:
                continue
            command = [str(ROOT / venv / "bin/python"), "-u", str(runner), "--brain", brain,
                       "--phase", phase, "--out-dir", str(args.out_root / phase / brain), "--timeout", str(args.timeout)]
            if args.resume and (args.out_root / phase / brain / "metadata.json").exists():
                command.append("--resume")
            if args.runtime_fix_audit:
                command.extend(["--runtime-fix-audit", str(args.runtime_fix_audit)])
            row = {"phase":phase, "brain":brain, "command":command, "started_at":datetime.now(timezone.utc).isoformat()}
            manifest["jobs"].append(row)
            print("START", phase, brain, flush=True)
            with (args.out_root / f"{phase}-{brain}.log").open("a" if args.resume else "x") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=environment)
                row["pid"] = process.pid
                save()
                row["exit_code"] = process.wait()
            row["finished_at"] = datetime.now(timezone.utc).isoformat()
            save()
            print("FINISH", phase, brain, row["exit_code"], flush=True)
            if row["exit_code"]:
                sys.exit(row["exit_code"])
    manifest["complete"] = True
    save()


if __name__ == "__main__":
    main()
