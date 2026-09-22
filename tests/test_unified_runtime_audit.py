import hashlib
import json

import pytest

from scripts import unified_runtime_audit as audit


def test_runtime_repair_requires_matching_code_and_unchanged_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    evidence = tmp_path / "episode.json"
    evidence.write_text('{"status":"success"}')
    proof = {"complete": True, "before_fingerprint": "old", "after_fingerprint": "new",
             "saved_results_unchanged": True, "live_page_verified": True,
             "preserved_files": [{"path": "episode.json", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]}
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(proof))
    assert audit.validate_runtime_audit(path, "new") == proof
    with pytest.raises(ValueError, match="current_code"):
        audit.validate_runtime_audit(path, "different")
    evidence.write_text('{"status":"fail"}')
    with pytest.raises(ValueError, match="evidence_changed"):
        audit.validate_runtime_audit(path, "new")
