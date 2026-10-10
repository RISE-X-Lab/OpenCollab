"""Historical feedback retains narrow matching and its original source."""

from arc_light import compatibility


def scenario(given="visitor unauthenticated"):
    return {
        "name": "Create and View Repository Releases",
        "scenario_index": 1,
        "requirement_id": "REQ-release",
        "source_ids": ["REQ-release"],
        "given": given,
        "when": "opens a release",
        "then": "reads its description",
    }


def test_observation_only_applies_to_exact_matching_missing_field(monkeypatch):
    item = scenario()
    monkeypatch.setitem(compatibility.RELEASE_OBSERVATION, "scenario_sha256", compatibility.scenario_fingerprint(item))
    result = compatibility.enrich_contract({"evolution_checks": [item]})
    assert result["evaluation_observations"][0]["kind"] == "user_supplied_evaluation_observation"
    assert "playwright-report.json" in result["evaluation_observations"][0]["source"]
    assert result["evolution_checks"][0]["assertions"]["release_description"]["value"] == "Evolution release"
    result = compatibility.enrich_contract({"evolution_checks": [scenario("changed unknown GIVEN")]})
    assert not result["evaluation_observations"]
    assert "release_description" not in result["evolution_checks"][0]["assertions"]


def test_public_description_wins_over_historical_feedback():
    result = compatibility.enrich_contract(
        {"evolution_checks": [scenario("existing description `public description`")]}
    )
    assert result["evolution_checks"][0]["assertions"]["release_description"] == {
        "value": "public description",
        "source": "public GIVEN",
    }
    assert not result["evaluation_observations"]
