"""Original requirement identity and coverage survive adapter selection."""

import json

import pytest
from arc_light.completion import scenario_coverage
from arc_light.evidence import improvement, repair_frontier, select_contract
from arc_light.public_checks import write_public_checks


def feature(identifier, name="Manage Active Browser Sessions", *, username="alice", scenarios=True):
    return {
        "id": identifier,
        "name": name,
        "scenarios": [
            {
                "steps": [
                    {
                        "keyword": "GIVEN",
                        "content": f"account `{username}` (`{username}@example.test`, `test-password`)",
                    },
                    {"keyword": "WHEN", "content": "Open active sessions"},
                    {"keyword": "THEN", "content": "The sessions are visible"},
                ]
            }
        ]
        if scenarios
        else [],
    }


def passed(item):
    return dict(item, status="passed", ok=True)


def test_same_feature_names_retain_original_identity_and_credentials(tmp_path):
    contract = write_public_checks([feature("REQ-A"), feature("REQ-B", username="bob")], tmp_path)
    selected = contract["evolution_checks"]
    assert [item["requirement_id"] for item in selected] == ["REQ-A", "REQ-B"]
    assert [item["account"]["username"] for item in selected] == ["alice", "bob"]
    assert selected[0]["scenario_id"] != selected[1]["scenario_id"]
    coverage = scenario_coverage(contract["required_scenarios"], [passed(selected[0])])
    assert coverage["passed"] == 1 and coverage["unverified"] == 1
    assert not coverage["complete"]
    assert scenario_coverage(contract["required_scenarios"], list(map(passed, selected)))["complete"]


@pytest.mark.parametrize("names", [["Unknown action"], ["Manage Active Browser Sessions", "Unknown action"]])
def test_unmapped_requirements_remain_in_requested_denominator(tmp_path, names):
    contract = write_public_checks([feature(f"REQ-{n}", name) for n, name in enumerate(names)], tmp_path)
    coverage = scenario_coverage(contract["required_scenarios"], list(map(passed, contract["evolution_checks"])))
    assert coverage["expected"] == len(names)
    assert coverage["mapped"] == len(names) - 1
    assert not coverage["complete"]
    assert any(row["reason"] == "unmapped_requirement" for row in coverage["scenarios"])


def test_no_public_scenarios_and_zero_execution_are_unverified(tmp_path):
    contract = write_public_checks([feature("REQ-EMPTY", scenarios=False)], tmp_path)
    coverage = scenario_coverage(
        contract["required_scenarios"], [], requirements_without_scenarios=contract["requirements_without_scenarios"]
    )
    assert coverage["expected"] == 0 and not coverage["complete"]
    assert coverage["requirements_without_scenarios"] == ["REQ-EMPTY"]
    assert not scenario_coverage([], [])["complete"]


def test_duplicate_result_and_display_name_cannot_cover_another_id(tmp_path):
    contract = write_public_checks([feature("REQ-A"), feature("REQ-B")], tmp_path)
    row = passed(contract["evolution_checks"][0])
    coverage = scenario_coverage(contract["required_scenarios"], [row, row])
    assert coverage["passed"] == 0
    assert [r["reason"] for r in coverage["scenarios"]] == ["duplicate_results", "not_executed"]


def test_original_scope_filters_by_id_and_rejects_missing_inputs(tmp_path):
    contract = write_public_checks([feature("REQ-A"), feature("REQ-B")], tmp_path, requirement_ids=["REQ-B"])
    assert contract["required_requirement_ids"] == ["REQ-B"]
    assert contract["required_scenarios"][0]["requirement_id"] == "REQ-B"
    with pytest.raises(ValueError, match="in-scope"):
        select_contract(contract, "requirements", ["REQ-A"])
    with pytest.raises(ValueError):
        write_public_checks([], tmp_path)
    with pytest.raises(ValueError):
        write_public_checks([feature("REQ-A"), feature("REQ-A")], tmp_path)
    assert json.loads((tmp_path / ".arc/checks/scenario-ledger.json").read_text())["expected"] == 1


def test_runtime_error_preserves_independent_failure_and_requires_comparable_recovery():
    before = {
        "ok": False,
        "scope": "all",
        "browser": {
            "server_errors": [{"status": 500}],
            "prerequisites": [
                {"name": "first", "status": "failed", "source_ids": ["REQ-A"]},
                {"name": "second", "status": "failed", "source_ids": ["REQ-B"]},
            ],
        },
    }
    assert {card["name"] for card in repair_frontier(before)} == {"first", "second", "browser_runtime"}
    after = {"ok": False, "scope": "all", "browser": {"prerequisites": before["browser"]["prerequisites"]}}
    assert improvement(before, after)
    after["browser"]["prerequisites"] = [{"name": "first", "status": "skipped"}]
    assert not improvement(before, after)


def test_same_name_failures_and_progress_keep_each_scenario_identity(tmp_path):
    contract = write_public_checks([feature("REQ-A"), feature("REQ-B")], tmp_path)
    rows = [
        dict(item, name="evolution:Manage Active Browser Sessions:scenario_1", status="failed")
        for item in contract["evolution_checks"]
    ]
    before = {"ok": False, "browser": {"prerequisites": rows}}
    assert {card["scenario_id"] for card in repair_frontier(before)} == {"REQ-A:scenario_1", "REQ-B:scenario_1"}
    after = {"ok": False, "browser": {"prerequisites": [dict(rows[0], status="passed"), rows[1]]}}
    assert improvement(before, after)
    replacement = {"ok": False, "browser": {"prerequisites": [rows[0], dict(rows[1], status="passed")]}}
    assert improvement(after, replacement)
