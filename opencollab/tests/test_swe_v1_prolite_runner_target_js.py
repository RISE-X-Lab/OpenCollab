from __future__ import annotations

# ruff: noqa: F401, F403, F405, I001

import hashlib
import http.server
import importlib.util
import inspect
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe_v1_prolite_runner_test_support import *


def test_jest_command_uses_workspace_config_and_canonical_test_path():
    namespace = _command_namespace()

    files = namespace["canonical_js_test_files"](
        ["src/app/utils/replaceLocalURL.test.ts | should replace"],
        [
            "applications/drive/src/app/utils/replaceLocalURL.test.ts",
            "src/app/utils/replaceLocalURL.test.ts",
        ],
    )
    assert files == ["applications/drive/src/app/utils/replaceLocalURL.test.ts"]

    command = namespace["jest_test_command"](files)
    assert "--json" in command
    assert "--coverage=false" in command
    assert "--config applications/drive/jest.config.js" in command
    assert "--runTestsByPath applications/drive/src/app/utils/replaceLocalURL.test.ts" in command


def test_nodebb_mocha_command_forces_named_test_output():
    namespace = _command_namespace()

    command = namespace["prolite_test_command"](
        {
            "repo": "NodeBB/NodeBB",
            "repo_language": "javascript",
            "selected_test_files_to_run": ["test/topics.js"],
        },
        ["test/topics.js | Topic's order pinned topics should order pinned topics"],
    )

    assert "--reporter json-stream" in command
    assert "--grep" in command
    assert "undeclared failing test" not in command
    assert "test/topics.js" in command
    grep_line = next(line for line in command.splitlines() if "--grep" in line)
    tokens = shlex.split(grep_line.strip())
    selector = tokens[tokens.index("--grep") + 1]
    title = "Topic's order pinned topics should order pinned topics"
    assert re.fullmatch(selector, title)
    assert re.fullmatch(selector, title + " but not this suffix") is None


def _nodebb_runner_namespace():
    namespace = _command_namespace()
    return namespace


def _install_fake_mocha(tmp_path: Path) -> Path:
    binary = tmp_path / "node_modules" / ".bin" / "mocha"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

with pathlib.Path(os.environ["MOCHA_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
test_file = sys.argv[-1]
print('["end",{"tests":1,"passes":1,"failures":0}]')
raise SystemExit(3 if test_file == os.environ.get("MOCHA_FAIL_FILE") else 0)
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def test_nodebb_target_file_with_colons_stays_on_mocha_path(tmp_path: Path):
    namespace = _nodebb_runner_namespace()
    target_file = tmp_path / "targets.json"
    target_file.write_text(
        json.dumps(["test/topics.js | suite::case should pass"]),
        encoding="utf-8",
    )

    command = namespace["prolite_test_command"](
        {
            "repo": "NodeBB/NodeBB",
            "repo_language": "javascript",
            "selected_test_files_to_run": ["test/topics.js"],
        },
        ["test/topics.js | suite::case should pass"],
        str(target_file),
    )

    assert "python3 -m pytest" not in command
    assert str(target_file) in command
    assert "missing declared Mocha titles" in command


@pytest.mark.parametrize("title_count", [111, 271])
def test_nodebb_target_file_runs_one_mocha_process_per_file(
    tmp_path: Path,
    title_count: int,
):
    namespace = _nodebb_runner_namespace()
    _install_fake_mocha(tmp_path)
    calls = tmp_path / "mocha-calls.jsonl"
    target_file = tmp_path / "targets.json"
    titles = [f"test/topics.js | stateful case {index:03d}" for index in range(title_count)]
    target_file.write_text(json.dumps(titles), encoding="utf-8")
    command = namespace["mocha_test_command"](titles, ["test/topics.js"], str(target_file))

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={**os.environ, "MOCHA_CALLS": str(calls)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(invocations) == 1
    arguments = invocations[0]
    assert arguments[-1] == "test/topics.js"
    selector = arguments[arguments.index("--grep") + 1]
    assert all(re.fullmatch(selector, f"stateful case {index:03d}") for index in range(title_count))
    assert len(command) < 3000


def test_nodebb_target_file_continues_after_one_file_fails(tmp_path: Path):
    namespace = _nodebb_runner_namespace()
    _install_fake_mocha(tmp_path)
    calls = tmp_path / "mocha-calls.jsonl"
    target_file = tmp_path / "targets.json"
    titles = [
        "test/a.js | first case",
        "test/b.js | second case",
    ]
    target_file.write_text(json.dumps(titles), encoding="utf-8")
    command = namespace["mocha_test_command"](titles, ["test/a.js", "test/b.js"], str(target_file))

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "MOCHA_CALLS": str(calls),
            "MOCHA_FAIL_FILE": "test/a.js",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 3
    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert [arguments[-1] for arguments in invocations] == ["test/a.js", "test/b.js"]


def test_mocha_json_stream_output_proves_named_javascript_tests():
    namespace = _proof_namespace()
    expected = [
        "test/topics.js | Topic's order pinned topics should error with unprivileged user",
        "test/topics.js | Topic's order pinned topics should order pinned topics",
    ]
    log = "\n".join(
        [
            '["start",{"total":188}]',
            '["pass",{"title":"should error with unprivileged user",'
            '"fullTitle":"Topic\'s order pinned topics should error with unprivileged user"}]',
            '["pass",{"title":"should order pinned topics",'
            '"fullTitle":"Topic\'s order pinned topics should order pinned topics"}]',
            '["end",{"suites":35,"tests":188,"passes":188,"failures":0}]',
        ]
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "javascript", "repo": "NodeBB/NodeBB"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected

    assert proof["missing"] == []


def test_jest_verbose_output_proves_uniquely_named_javascript_tests():
    namespace = _proof_namespace()
    expected = [
        "src/usePhotosRecovery.test.ts | usePhotosRecovery should pass all state",
        "src/usePhotosRecovery.test.ts | usePhotosRecovery should report move errors",
    ]
    log = """PASS src/usePhotosRecovery.test.ts
  usePhotosRecovery
    ✓ should pass all state (70 ms)
    ✓ should report move errors (12 ms)

Test Suites: 1 passed, 1 total
Tests:       2 passed, 2 total
"""

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "typescript", "repo": "ProtonMail/WebClients"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []


def test_jest_json_maps_unique_contiguous_abbreviated_titles():
    namespace = _proof_namespace()
    test_file = "test/voice-broadcast/stores/VoiceBroadcastPreRecordingStore-test.ts"
    expected = [
        f"{test_file} | VoiceBroadcastPreRecordingStore | getCurrent",
        f"{test_file} | VoiceBroadcastPreRecordingStore | clearCurrent",
        f"{test_file} | when setting a current recording | getCurrent",
        f"{test_file} | and setting another pre-recording | getCurrent",
    ]
    assertions = [
        (["VoiceBroadcastPreRecordingStore"], "getCurrent() should return null"),
        (["VoiceBroadcastPreRecordingStore"], "clearCurrent() should work"),
        (
            ["VoiceBroadcastPreRecordingStore", "when setting a current recording"],
            "getCurrent() should return the recording",
        ),
        (
            [
                "VoiceBroadcastPreRecordingStore",
                "when setting a current recording",
                "and setting another pre-recording",
            ],
            "getCurrent() should return the new recording",
        ),
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/" + test_file,
                    "assertionResults": [
                        {
                            "ancestorTitles": ancestor_titles,
                            "title": title,
                            "fullName": " ".join([*ancestor_titles, title]),
                            "status": "passed",
                        }
                        for ancestor_titles, title in assertions
                    ],
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "typescript", "repo": "element-hq/element-web"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []


def test_jest_json_does_not_match_abbreviation_across_nested_title_level():
    namespace = _proof_namespace()
    test_file = "test/store.test.ts"
    expected = [f"{test_file} | Store | getCurrent"]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/" + test_file,
                    "assertionResults": [
                        {
                            "ancestorTitles": ["Store", "when populated"],
                            "title": "getCurrent() returns the value",
                            "fullName": "Store when populated getCurrent() returns the value",
                            "status": "passed",
                        }
                    ],
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "typescript", "repo": "element-hq/element-web"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is False
    assert proof["observed"] == []
    assert proof["missing"] == expected


def test_jest_command_groups_component_tests_under_component_config():
    namespace = _command_namespace()

    files = namespace["canonical_js_test_files"](
        ["containers/payments/RenewalNotice.test.tsx | should render"],
        [
            "packages/components/containers/payments/RenewalNotice.test.tsx",
            "containers/payments/RenewalNotice.test.tsx",
        ],
    )
    command = namespace["jest_test_command"](files)

    assert files == ["packages/components/containers/payments/RenewalNotice.test.tsx"]
    assert "--config packages/components/jest.config.js" in command
    assert "packages/components/containers/payments/RenewalNotice.test.tsx" in command


def test_jest_command_chains_multiple_workspaces_without_leading_and_operator():
    namespace = _command_namespace()

    command = namespace["jest_test_command"](
        [
            "applications/drive/src/drive.test.ts",
            "packages/components/src/component.test.ts",
        ]
    )

    assert "fi &&\nif" in command
    assert "\n&&\n" not in command


def test_jest_json_output_proves_named_javascript_tests():
    namespace = _proof_namespace()
    expected = [
        "src/example.test.ts | Example should pass",
        "src/example.test.ts | Example should fail",
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "assertionResults": [
                        {"fullName": "Example should pass", "status": "passed"},
                        {"fullName": "Example should fail", "status": "failed"},
                    ]
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "ProtonMail/WebClients"},
        expected,
        1,
        log,
    )

    assert proof["observed"] == expected
    assert proof["passed"] == [expected[0]]
    assert proof["failed"] == [expected[1]]
    assert proof["missing"] == []


def test_jest_json_proof_reads_results_after_more_than_four_megabytes():
    namespace = _proof_namespace()
    expected = ["src/large.test.ts | should finish"]
    event = json.dumps(
        {
            "testResults": [
                {
                    "assertionResults": [
                        {"fullName": "Large suite should finish", "status": "passed"}
                    ]
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "protonmail/webclients"},
        expected,
        0,
        "x" * 4_100_000 + "\n" + event,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert remote_state.MAX_TEST_EVIDENCE_BYTES >= 64_000_000


def test_jest_json_full_name_maps_unique_nested_titles_without_false_matches():
    namespace = _proof_namespace()
    expected = [
        "src/example.test.ts | localhost should not replace local URLs",
        "src/example.test.ts | proton.me should not replace local URLs",
        'src/example.test.ts | should display the expected fields for the "new invitation" happy case',
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "fullName": "replaceLocalURL localhost should not replace local URLs",
                            "status": "passed",
                        },
                        {
                            "fullName": "replaceLocalURL proton.me should not replace local URLs",
                            "status": "passed",
                        },
                        {
                            "fullName": (
                                'ICS widget organizer mode should display the expected fields for the '
                                '"new invitation" happy case'
                            ),
                            "status": "failed",
                        },
                    ]
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "protonmail/webclients"},
        expected,
        1,
        log,
    )

    assert proof["observed"] == expected
    assert proof["passed"] == expected[:2]
    assert proof["failed"] == [expected[2]]
    assert proof["missing"] == []


def test_jest_json_uses_test_file_to_disambiguate_repeated_titles():
    namespace = _proof_namespace()
    expected = [
        "test/a/example.test.ts | should render",
        "test/b/example.test.ts | should render",
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/a/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A should render", "status": "passed"}
                    ],
                },
                {
                    "name": "/app/test/b/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite B should render", "status": "passed"}
                    ],
                },
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"},
        expected,
        0,
        log,
    )
    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected

    only_a = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/a/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A should render", "status": "passed"}
                    ],
                }
            ]
        }
    )
    missing = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"}, expected, 0, only_a
    )
    assert missing["passed"] == [expected[0]]
    assert missing["missing"] == [expected[1]]

    wrong_file = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/b/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A unique title", "status": "passed"}
                    ],
                }
            ]
        }
    )
    unique_expected = ["test/a/example.test.ts | unique title"]
    wrong = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"},
        unique_expected,
        0,
        wrong_file,
    )
    assert wrong["observed"] == []
    assert wrong["missing"] == unique_expected

    a_pass_b_fail = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/a/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A should render", "status": "passed"}
                    ],
                },
                {
                    "name": "/app/test/b/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite B should render", "status": "failed"}
                    ],
                },
            ]
        }
    )
    mixed = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"},
        expected,
        1,
        a_pass_b_fail,
    )
    assert mixed["passed"] == [expected[0]]
    assert mixed["failed"] == [expected[1]]


def test_jest_json_normalizes_multiple_declared_title_levels():
    namespace = _proof_namespace()
    expected = [
        "test/recovery.test.ts | flow to set up recovery | should display the recovery key",
        "test/recovery.test.ts | flow to change recovery | should display the recovery key",
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/recovery.test.ts",
                    "assertionResults": [
                        {
                            "fullName": "Recovery flow to set up recovery should display the recovery key",
                            "status": "passed",
                        },
                        {
                            "fullName": "Recovery flow to change recovery should display the recovery key",
                            "status": "passed",
                        },
                    ],
                }
            ]
        }
    )
    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "protonmail/webclients"}, expected, 0, log
    )
    assert proof["ok"] is True
    assert proof["passed"] == expected


def test_tutanota_uses_real_test_runner_and_proves_completed_suites():
    command_namespace = _command_namespace()
    expected = [
        "test/tests/api/worker/rest/EntityRestClientTest.js | test suite",
        "test/tests/api/worker/rest/ServiceExecutorTest.js | test suite",
    ]
    command = command_namespace["prolite_test_command"](
        {"repo": "tutao/tutanota", "repo_language": "ts", "selected_test_files_to_run": []},
        expected,
    )

    assert "OPENCOLLAB_OSPEC_RESULTS" in command
    assert "EntityRestClient" in command
    assert "ServiceExecutor" in command
    assert "opencollabResults" in command
    assert command.endswith("&& npm_config_nodedir=/usr/local npm run test:app")
    assert command.index("const errCount = o.report(results, stats)") < command.index(
        "OPENCOLLAB_OSPEC_RESULTS"
    )
    assert command_namespace["prolite_test_command"](
        {"repo": "tutao/tutanota", "repo_language": "ts", "selected_test_files_to_run": []},
        [],
    ) == ""

    proof_namespace = _proof_namespace()
    proof = proof_namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "tutao/tutanota"},
        expected,
        0,
        "OPENCOLLAB_OSPEC_RESULTS "
        + json.dumps(
            [
                {"task": "loads", "context": ["EntityRestClient", "Load"], "pass": True},
                {"task": "posts", "context": ["ServiceExecutor", "POST"], "pass": True},
            ]
        ),
    )
    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []
