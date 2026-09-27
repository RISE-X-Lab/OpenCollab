"""Observe native shell commands and retain parser-backed test evidence."""

from ._test_results import has_pass_evidence
from .bash_evidence import BashEvidence

__all__ = ["BashEvidence", "has_pass_evidence"]
