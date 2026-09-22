"""Require a concrete, hashed validation record for a resumed runtime repair."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_runtime_audit(path, current_fingerprint):
    proof = json.loads(Path(path).read_text())
    if not proof.get("complete") or proof["after_fingerprint"] != current_fingerprint:
        raise ValueError("runtime_fix_audit_does_not_cover_current_code")
    if not proof.get("saved_results_unchanged") or not proof.get("live_page_verified"):
        raise ValueError("runtime_fix_audit_missing_verification")
    for item in proof["preserved_files"]:
        if hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("runtime_fix_preserved_evidence_changed:" + item["path"])
    return proof
