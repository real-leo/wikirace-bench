"""Exercise the actual browser JavaScript at astral-character slice boundaries."""
import json
import shutil
import subprocess

import pytest

from wikirace.browser import _SAFE_SLICE_JS
from scripts.run_unified_retest import save


def test_browser_slice_preserves_complete_math_characters_and_existing_budgets():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the browser JavaScript regression")
    values = ["abc", "a𝜔b", "x😀y", "𝜔😀"]
    cases = [(s, a, b) for s in values for a in range(len(s.encode("utf-16-le")) // 2 + 1)
             for b in range(a, len(s.encode("utf-16-le")) // 2 + 1)]
    script = _SAFE_SLICE_JS + "\nconst cases = " + json.dumps(cases) + ";\n" + (
        "process.stdout.write(JSON.stringify(cases.map(([s,a,b]) => safeSlice(s,a,b))));")
    output = json.loads(subprocess.check_output([node, "-e", script], text=True))
    for (value, start, end), actual in zip(cases, output):
        actual.encode("utf-8")  # No unpaired surrogate survives.
        raw = value.encode("utf-16-le")[start * 2:end * 2]
        assert actual == raw.decode("utf-16-le", errors="ignore")


def test_failed_episode_json_preserves_even_unpaired_surrogates(tmp_path):
    row = {"context": "math \udf14", "title": "𝜔 中文", "error": "encoding failure"}
    target = tmp_path / "episode.json"
    save(target, row)
    assert json.loads(target.read_text(encoding="utf-8")) == row
    assert not target.with_suffix(".json.tmp").exists()
