from scripts.summarize_page_retest import checkpoints


def test_checkpoints_use_action_completion_time_and_do_not_restart():
    row = {"status": "success", "reason": "goal_reached", "seconds": 620,
           "clicks": 3, "timeout_s": 3600, "trace": [
               {"elapsed_seconds": 590, "action_elapsed_seconds": 589, "clicks_so_far": 2, "chosen_title": "Bridge"},
               {"elapsed_seconds": 620, "action_elapsed_seconds": 619, "clicks_so_far": 3, "chosen_title": "Goal"}]}
    result = checkpoints(row)
    assert [r["clicks"] for r in result] == [2, 3, 3]
    assert [r["status"] for r in result] == ["not_reached_yet", "success", "success"]


def test_final_timeout_with_small_clock_overshoot_is_finished_at_budget():
    row = {"status": "fail", "reason": "timeout", "seconds": 3600.3,
           "clicks": 100, "timeout_s": 3600, "trace": [
               {"elapsed_seconds": 3600.3, "action_elapsed_seconds": 3599.9,
                "clicks_so_far": 100, "chosen_title": "Still not goal"}]}
    assert checkpoints(row)[-1]["status"] == "fail"


def test_goal_arrival_before_cutoff_counts_even_if_final_logging_crosses_it():
    row = {"status": "success", "reason": "reached_goal", "seconds": 600.3,
           "clicks": 4, "timeout_s": 3600, "trace": [
               {"elapsed_seconds": 600.2, "action_elapsed_seconds": 599.9,
                "clicks_so_far": 4, "chosen_title": "Goal"}]}
    assert checkpoints(row)[0]["status"] == "success"
