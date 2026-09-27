"""Output-proof regressions retained after removing the dedicated test tool."""
import pytest

from opencollab.adapters.tools.evidence.django_evidence import DJANGO_RUNNER
from opencollab.tools import has_pass_evidence

PASS = "test_one (admin_utils.tests.Example.test_one) ... ok\n\nRan 1 test in 0.001s\n\nOK\n"


@pytest.mark.parametrize("target", ["admin_utils", "tests/admin_utils", "admin_utils.tests.Example.test_one",
                                   "tests/admin_utils/tests.py::Example::test_one"])
def test_django_accepts_matching_executed_labels(target):
    assert has_pass_evidence(0, PASS, runner=DJANGO_RUNNER, target=target)


@pytest.mark.parametrize("output", [
    "Ran 0 tests in 0.001s\nOK\n", "Ran 1 test in 0.001s\nOK\n",
    PASS.replace(" ... ok", " ... skipped 'disabled'").replace("OK", "OK (skipped=1)"),
    PASS.replace(" ... ok", " ... FAIL").replace("OK", "FAILED (failures=1)"),
    PASS + "Ran 0 tests in 0.01s\nOK\n", PASS.replace("Ran 1 test", "Ran 2 tests"),
    PASS + "FAILED (errors=1)\n", "usage: runtests.py --help\n", PASS + PASS,
])
def test_django_rejects_missing_ambiguous_or_nonpassing_execution(output):
    assert not has_pass_evidence(0, output, runner=DJANGO_RUNNER, target="admin_utils")


def test_django_rejects_wrong_target_and_nonzero_exit():
    assert not has_pass_evidence(0, PASS, runner=DJANGO_RUNNER, target="auth_tests")
    assert not has_pass_evidence(1, PASS, runner=DJANGO_RUNNER, target="admin_utils")


def test_evidence_rejects_multiple_pytest_result_summaries():
    from opencollab.tools import has_pass_evidence

    target = "tests/test_x.py::test_one"
    output = (
        f"FAILED {target} - assertion failed\n"
        "1 failed in 0.01s\n"
        f"PASSED {target}\n"
        "1 passed in 0.01s\n"
    )

    assert not has_pass_evidence(0, output, target=target)


def test_go_runner_multi_target_forgery_cannot_produce_green():
    forged_output = '{"Action":"pass","Package":"module/pkg1","Test":"TestB"}'

    assert not has_pass_evidence(
        0,
        forged_output,
        runner="go test",
        target="./pkg1::TestA ./pkg2::TestB",
    )


def test_go_runner_single_selector_requires_exact_test_proof():
    wrong_test = '{"Action":"pass","Package":"module/pkg1","Test":"TestB"}'
    exact_test = '{"Action":"pass","Package":"module/pkg1","Test":"TestA"}'
    subtest = '{"Action":"pass","Package":"module/pkg1","Test":"TestA/subcase"}'
    target = "./pkg1::TestA"

    assert not has_pass_evidence(0, wrong_test, runner="go test", target=target)
    assert has_pass_evidence(0, exact_test, runner="go test", target=target)
    assert has_pass_evidence(0, subtest, runner="go test", target=target)


def test_root_level_pytest_selector_accepts_matching_node_from_any_file():
    from opencollab.tools import has_pass_evidence

    output = "PASSED tests/test_x.py::test_one\n1 passed in 0.01s\n"

    assert has_pass_evidence(0, output, target="::test_one")
    assert not has_pass_evidence(0, output, target="::test_other")


def test_go_runner_requires_pass_proof_from_requested_package():
    from opencollab.tools import has_pass_evidence

    unrelated_output = (
        '{"Action":"pass","Package":"module/internal/other","Test":"TestEvaluate"}'
    )
    matching_output = (
        '{"Action":"pass","Package":"module/internal/server","Test":"TestEvaluate"}'
    )

    assert not has_pass_evidence(
        0,
        unrelated_output,
        runner="go test",
        target="internal/server",
    )
    assert has_pass_evidence(
        0,
        matching_output,
        runner="go test",
        target="internal/server",
    )


def test_go_runner_requires_pass_proof_for_each_requested_package():
    from opencollab.tools import has_pass_evidence

    one_package = (
        '{"Action":"pass","Package":"module/internal/server","Test":"TestEvaluate"}'
    )
    both_packages = "\n".join(
        [
            one_package,
            '{"Action":"pass","Package":"module/rpc/flipt","Test":"TestRPC"}',
        ]
    )
    target = "./internal/server ./rpc/flipt"

    assert not has_pass_evidence(0, one_package, runner="go test", target=target)
    assert has_pass_evidence(0, both_packages, runner="go test", target=target)
