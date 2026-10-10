"""Fresh-copy streaks remain specific to original scenario identity."""

from arc_light.stability import observe, read_history, targets


def result(identifier, status):
    return {
        "browser": {
            "prerequisites": [
                {
                    "name": "evolution:Freeze Rows and Columns:scenario_3",
                    "requirement_id": identifier,
                    "scenario_index": 2,
                    "scenario_id": f"{identifier}:scenario_3",
                    "status": status,
                    "source_ids": [identifier],
                }
            ]
        }
    }


def test_failure_between_successes_resets_only_its_scenario(tmp_path):
    observe(tmp_path, result("REQ-A", "passed"), "source", tmp_path / "first.json")
    observe(tmp_path, result("REQ-B", "passed"), "source", tmp_path / "second.json")
    observe(tmp_path, result("REQ-A", "failed"), "source", tmp_path / "third.json")
    observe(tmp_path, result("REQ-A", "passed"), "source", tmp_path / "fourth.json")
    history = read_history(tmp_path)
    assert history["checks"]["REQ-A:scenario_3"]["consecutive_passes"] == 1
    assert history["checks"]["REQ-A:scenario_3"]["failure_count"] == 1
    assert history["checks"]["REQ-B:scenario_3"]["consecutive_passes"] == 1
    contract = {
        "evolution_checks": [
            {"name": "Freeze Rows and Columns", "scenario_index": 2, "requirement_id": key, "source_ids": [key]}
            for key in ("REQ-A", "REQ-B")
        ]
    }
    assert len(targets(contract, history)) == 2


def test_duplicate_results_reset_streak_instead_of_manufacturing_fresh_passes(tmp_path):
    report = result("REQ-A", "passed")
    report["browser"]["prerequisites"] *= 2
    observe(tmp_path, report, "source", tmp_path / "duplicate.json")
    assert read_history(tmp_path)["checks"]["REQ-A:scenario_3"]["consecutive_passes"] == 0
