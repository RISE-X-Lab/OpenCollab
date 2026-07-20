"""Unit tests for the domain Topology value object."""

from __future__ import annotations

from opencollab.domain.team import Topology


def test_allow_all_permits_every_edge():
    topo = Topology(allow_all=True)
    assert topo.allows("lead", "coder")
    assert topo.allows("anyone", "anyone-else")


def test_explicit_edges_permit_only_listed_targets():
    topo = Topology(edges={"lead": frozenset({"coder", "reviewer"}), "coder": frozenset({"reviewer"})})
    assert topo.allows("lead", "coder")
    assert topo.allows("lead", "reviewer")
    assert topo.allows("coder", "reviewer")


def test_unlisted_edge_is_denied():
    topo = Topology(edges={"lead": frozenset({"coder"})})
    assert not topo.allows("lead", "reviewer")
    assert not topo.allows("coder", "lead")  # coder has no entry at all


def test_empty_topology_denies_everything():
    topo = Topology()
    assert not topo.allows("lead", "coder")
