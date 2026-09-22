"""Run Jev, SemIf and Laya sequentially with a shared extended action budget."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    args.out_root.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "USE_TF": "0"}
    jobs = [("jev", ".venv", "score-255"), ("semif", ".venv-semif", "grouped-all"),
            ("laya-mlx", ".venv-mlx", "grouped-all")]
    runner = ROOT / "scripts/run_mlx_retest.py"
    manifest = {"timeout_per_task_seconds": args.timeout, "gpu_runs": "sequential",
                "task_file": "data/tasks_hard.json", "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                "tasks_sha256": hashlib.sha256((ROOT / "data/tasks_hard.json").read_bytes()).hexdigest(),
                "jobs": []}
    manifest_path = args.out_root / "manifest.json"

    def save():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    for brain, venv, policy in jobs:
        command = [str(ROOT / venv / "bin/python"), "-u", str(runner), "--brain", brain,
                   "--page-policy", policy, "--out-dir", str(args.out_root / brain),
                   "--timeout", str(args.timeout)]
        row = {"brain": brain, "policy": policy, "command": command,
               "started_at": datetime.now(timezone.utc).isoformat(), "status": "running"}
        manifest["jobs"].append(row)
        print(f"START {brain} timeout={args.timeout}", flush=True)
        with (args.out_root / (brain + ".log")).open("x") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=environment)
            row["pid"] = process.pid
            save()
            row["exit_code"] = process.wait()
        row.update(status="finished", finished_at=datetime.now(timezone.utc).isoformat())
        save()
        print(f"FINISHED {brain} exit_code={row['exit_code']}", flush=True)
    sys.exit(int(any(job["exit_code"] for job in manifest["jobs"])))


if __name__ == "__main__":
    main()
