"""Probe Gemini and run its three live races; optionally wait for the baseline."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.report_gemini_comparison import render, verify_probe
from scripts.run_unified_retest import save
from wikirace.eval import _code_fingerprint


def read_manifest(path):
    # The older controller writes its manifest in place.
    for attempt in range(3):
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            if attempt == 2:
                raise
            time.sleep(0.2)


def baseline_ready(root):
    manifest = read_manifest(root / "manifest.json")
    if manifest.get("complete"):
        return True
    active = [j for j in manifest.get("jobs", []) if "exit_code" not in j]
    for job in active:
        try:
            os.kill(job["pid"], 0)
            return False
        except ProcessLookupError:
            pass
    raise RuntimeError("existing_benchmark_stopped_before_completion")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--wait-for-baseline", action="store_true")
    parser.add_argument("--reuse-probe", type=Path,
                        help="Reuse a complete Gemini probe; its canonical and HTTP inputs are re-audited")
    parser.add_argument("--resume-after-probe", action="store_true",
                        help="Resume a stopped, queued controller without repeating completed API probes")
    args = parser.parse_args()
    if args.reuse_probe and args.resume_after_probe:
        raise ValueError("reuse_probe_and_resume_are_mutually_exclusive")
    args.out_root.mkdir(parents=True, exist_ok=True)
    adapter_hash = hashlib.sha256((ROOT / "scripts/unified_chat_brain.py").read_bytes()).hexdigest()
    if args.resume_after_probe:
        manifest = read_manifest(args.out_root / "manifest.json")
        if manifest["phase"] != "waiting_for_baseline" or any(j["phase"] == "race" for j in manifest["jobs"]):
            raise ValueError("resume_requires_completed_probe_and_unstarted_races")
        if (manifest["code_fingerprint"] != _code_fingerprint() or manifest["adapter_sha256"] != adapter_hash
                or manifest["timeout_per_task"] != args.timeout or manifest["baseline_root"] != str(args.baseline_root)):
            raise ValueError("resume_configuration_changed")
        try:
            os.kill(manifest["controller_pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("previous_controller_is_still_running")
        manifest.setdefault("previous_controller_pids", []).append(manifest["controller_pid"])
        manifest["controller_pid"] = os.getpid()
        manifest["resumed_at"] = datetime.now(timezone.utc).isoformat()
    else:
        if (args.out_root / "manifest.json").exists():
            raise ValueError("output_already_contains_a_comparison")
        manifest = {"phase": "probe", "complete": False, "controller_pid": os.getpid(), "jobs": [],
                "started_at": datetime.now(timezone.utc).isoformat(), "baseline_root": str(args.baseline_root),
                "timeout_per_task": args.timeout, "code_fingerprint": _code_fingerprint(),
                "adapter_sha256": adapter_hash,
                "fixed_probe_overlaps_local_benchmark": not read_manifest(args.baseline_root / "manifest.json").get("complete", False)}
    manifest["live_race_waits_for_baseline"] = args.wait_for_baseline
    if args.reuse_probe:
        source = json.loads(args.reuse_probe.read_text())
        if not source.get("complete") or source.get("model") != "models/gemini-3.8-flash":
            raise ValueError("reused_probe_must_be_complete_for_requested_model")
        target = args.out_root / "probe/gemini"
        target.mkdir(parents=True, exist_ok=False)
        shutil.copy2(args.reuse_probe, target / "probe.json")
        manifest["reused_probe_from"] = str(args.reuse_probe)
    def update():
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        save(args.out_root / "manifest.json", manifest)
        render(args.out_root, args.baseline_root, manifest)
    def run(phase):
        manifest["phase"] = phase
        command = [sys.executable, "-u", str(ROOT / "scripts/run_unified_retest.py"),
                   "--brain", "gemini", "--phase", phase, "--out-dir", str(args.out_root / phase / "gemini"),
                   "--timeout", str(args.timeout)]
        job = {"phase": phase, "command": command, "started_at": datetime.now(timezone.utc).isoformat()}
        manifest["jobs"].append(job)
        print("START", phase, flush=True)
        with (args.out_root / (phase + "-gemini.log")).open("x") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
            job["pid"] = child.pid
            try:
                while child.poll() is None:
                    update()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=15)
            job["exit_code"] = child.returncode
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        if child.returncode:
            raise RuntimeError(f"gemini_{phase}_exited_{child.returncode}; see {phase}-gemini.log")
        update()
        print("FINISH", phase, flush=True)
    try:
        update()
        if not args.resume_after_probe and not args.reuse_probe:
            run("probe")
        probe = json.loads((args.out_root / "probe/gemini/probe.json").read_text())
        old = [json.loads((args.baseline_root / "probe" / name / "probe.json").read_text())
               for name in ("jev", "laya-mlx", "semif")]
        verify_probe(probe, old)
        manifest["fixed_input_parity_verified"] = True
        if args.wait_for_baseline:
            manifest["phase"] = "waiting_for_baseline"
            update()
            print("FIXED_INPUT_PARITY_VERIFIED; waiting for existing benchmark", flush=True)
            while not baseline_ready(args.baseline_root):
                time.sleep(30)
                update()
        manifest["baseline_complete_before_live_race"] = bool(read_manifest(args.baseline_root / "manifest.json").get("complete"))
        manifest["live_race_overlaps_local_benchmark"] = not manifest["baseline_complete_before_live_race"]
        run("race")
        manifest.update(phase="complete", complete=True, finished_at=datetime.now(timezone.utc).isoformat())
        update()
        print("COMPLETE", flush=True)
    except Exception as exc:
        manifest.update(phase="stopped", error=f"{type(exc).__name__}: {exc}")
        update()
        raise


if __name__ == "__main__":
    main()
