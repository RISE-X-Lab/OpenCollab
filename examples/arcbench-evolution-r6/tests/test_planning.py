"""Competition resource buckets retain the original requirement order through the core planner."""

from arc_light.planning import feature_groups, group_weight


def requirement(identifier, name, *, dependencies=(), scenarios=1, target=True):
    return {
        "id": identifier,
        "name": name,
        "atomic": target,
        "evolution_change": target,
        "scenario_count": scenarios,
        "dependencies": list(dependencies),
    }


def test_dependency_cycle_restores_interleaved_requirement_order():
    index = [
        requirement("REQ-A", "Freeze Rows and Columns", dependencies=("REQ-B",), scenarios=2),
        requirement("REQ-B", "View Organization Audit Log", dependencies=("REQ-C",)),
        requirement("REQ-C", "Find and Replace Cell Text", scenarios=3),
        requirement("REQ-D", "Manage Active Browser Sessions", dependencies=("REQ-B",)),
    ]
    groups = feature_groups(index)
    assert [group["ids"] for group in groups] == [["REQ-A", "REQ-B", "REQ-C"], ["REQ-D"]]
    assert groups[0]["rows"] == index[:3]
    assert groups[0]["resources"] == ["organization", "sheet_document_state"]
    assert groups[0]["dependencies"] == [] and groups[0]["dependency_cycle_merged"]
    assert groups[1]["dependencies"] == ["REQ-B"] and not groups[1]["dependency_cycle_merged"]
    assert group_weight(groups[0]) == 6


def test_interleaved_resource_buckets_keep_stable_ready_order_and_inherited_dependencies():
    index = [
        requirement("REQ-A", "Manage Active Browser Sessions"),
        requirement("REQ-B", "View Organization Audit Log", dependencies=("INHERITED",)),
        requirement("REQ-C", "Sign In with an Existing Account"),
        requirement("REQ-D", "Archive and Restore a Repository", dependencies=("REQ-B", "REQ-A")),
        requirement("REQ-E", "A separate feature"),
        requirement("INHERITED", "Existing parent", dependencies=("REQ-E",), target=False),
    ]
    groups = feature_groups(index)
    assert [group["ids"] for group in groups] == [["REQ-A", "REQ-C"], ["REQ-E"], ["REQ-B"], ["REQ-D"]]
    assert groups[0]["rows"] == [index[0], index[2]]
    assert groups[2]["dependencies"] == ["REQ-E"]
    assert groups[3]["dependencies"] == ["REQ-A", "REQ-B"]
    assert not any(group["dependency_cycle_merged"] for group in groups)
