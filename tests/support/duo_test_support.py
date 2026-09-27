"""Candidate and context doubles shared by Duo tests."""

from opencollab.workflows import CandidateRun


def candidate(label, value):
    return CandidateRun(
        label=label,
        output="Public repair completed",
        diff=("diff --git a/src/handler.py b/src/handler.py\n"
              "--- a/src/handler.py\n+++ b/src/handler.py\n"
              f"@@ -1 +1 @@\n-old\n+{value}\n"),
        test_records=(),
        verified_targets=(),
    )


def decision(evidence="src/handler.py implements the required return value"):
    return {
        "winner": "B",
        "requirements_complete": True,
        "requirements": [{
            "requirement": "Preserve the public return value",
            "a_coverage": "not_covered",
            "b_coverage": "covered",
            "a_evidence": ["src/handler.py retains the incorrect value"],
            "b_evidence": [evidence],
        }],
        "rationale": "B covers the requirement that A leaves unresolved",
    }


class Context:
    def __init__(self, *, result=None, barrier=None, identical=False):
        self.result = result or decision()
        self.barrier = barrier
        self.identical = identical
        self.coder_calls = []
        self.selector_calls = []
        self.adoptions = []
        self.phases = []

    async def candidate_agent(self, prompt, **options):
        if not self.coder_calls and self.barrier is not None:
            await self.barrier()
        self.coder_calls.append((prompt, options))
        value = "a" if self.identical or len(self.coder_calls) == 1 else "b"
        return candidate(options["label"], value)

    async def agent(self, prompt, **options):
        self.selector_calls.append((prompt, options))
        return self.result

    async def phase(self, title):
        self.phases.append(title)

    async def diff(self):
        return "[Working tree status]\n(clean)"

    async def adopt_candidate(self, selected, *, preserve_paths):
        self.adoptions.append((selected, preserve_paths))

    async def log(self, message):
        pass

    def tokens_spent(self):
        return 0
