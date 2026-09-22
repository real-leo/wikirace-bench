import json

from scripts.run_unified_retest import external_error, prepare_resume


def test_external_failures_do_not_mask_input_integrity_errors():
    assert external_error({"status":"error", "reason":"execute:PageLoadError: browser_network_error"})
    assert external_error({"status":"error", "reason":"choice:HTTPStatusError: 400 Bad Request"})
    assert not external_error({"status":"error", "reason":"choice:ValueError: semif_actual_input_differs_from_shared_contract"})
    assert not external_error({"status":"fail", "reason":"timeout"})


def test_resume_preserves_completed_results_and_archives_failure_evidence(tmp_path):
    tasks = [{"id":"done"},{"id":"failed"},{"id":"partial"},{"id":"not-started"}]
    complete = json.dumps({"status":"success", "clicks":5})
    (tmp_path/'done.json').write_text(complete)
    failed = json.dumps({"status":"error", "reason":"network error"})
    for suffix in ('.json','.summary.json'):
        (tmp_path/('failed'+suffix)).write_text(failed)
    (tmp_path/'failed.progress.jsonl').write_text('failed progress\n')
    (tmp_path/'partial.progress.jsonl').write_text('partial progress\n')
    pending = prepare_resume(tmp_path,tasks)
    assert [t['id'] for t in pending] == ['failed','partial','not-started']
    assert (tmp_path/'done.json').read_text() == complete
    assert (tmp_path/'attempts/failed/1/failed.json').read_text() == failed
    assert (tmp_path/'attempts/failed/1/failed.progress.jsonl').read_text() == 'failed progress\n'
    assert (tmp_path/'attempts/partial/1/partial.progress.jsonl').exists()
    assert not (tmp_path/'failed.json').exists()
