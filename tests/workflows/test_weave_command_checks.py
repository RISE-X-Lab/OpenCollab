"""Weave checks execute declared behavior and retain native test proof."""

from __future__ import annotations

import shlex
import shutil
import sys

import pytest

from opencollab import OpenCollab
from opencollab.builtin_workflows import WeaveAdapter
from opencollab.builtin_workflows.weave._execution import _static_printer_supplies, _verification_mode
from tests.workflows.test_weave_execution import FileModel


async def _check(workspace, command, seconds=10):
    async def flow(ctx, _args):
        check = await WeaveAdapter(check_commands=[command]).verify(ctx, None, seconds)
        return {"ok": check.ok, "executed": check.executed, "report": check.report}

    result = await OpenCollab(workspace, model="offline").workflow(flow, trace=False, timeout=20)
    assert result.ok, result.reason
    return result.output


@pytest.mark.parametrize("command,executable,supplied", [
    ("exec echo CHECK_OK", False, True),
    ("busybox echo CHECK_OK", False, True),
    ("bash -c 'echo CHECK_OK'", False, True),
    ("nice -n 0 echo CHECK_OK", False, True),
    ("echo CHECK_OK && python -c 'import sys; sys.exit(0)'", True, True),
    ("python -c 'print(\"CHECK_OK\")'", True, True),
    ("node -e 'console.log(\"CHECK_OK\")'", True, True),
    ("eval 'echo CHECK_OK'", False, True),
    ("env -i python check.py", False, False),
    ("echo 'Starting check' && python check.py", True, False),
    ("awk 'BEGIN{print \"CHECK_OK\"}'", False, True),
    ("awk 'BEGIN{print \"CHECK\" \"_OK\"}'", False, False),
    ("sed -n 's/.*/CHECK_OK/p'", False, True),
    ("python -c 'print(\"CHE\"+\"CK_OK\")'", True, True),
    ("python -c 'x=\"open(\"; print(\"CHE\"+\"CK_OK\")'", True, True),
    ("python -c 'print(open(\"result.txt\").read())'", True, True),
    ('python -c"print(\'CHE\'+\'CK_OK\')"', True, True),
    ('SCRIPT=\'print("CHE"+"CK_OK")\' python -c"$SCRIPT"', True, True),
    ("python -ic 'print(\"CHE\"+\"CK_OK\")'", True, True),
    ("python -bc 'print(\"CHE\"+\"CK_OK\")'", True, True),
    ("node -p '\"CHE\"+\"CK_OK\"'", True, True),
    ("perl -E 'say \"CHE\".\"CK_OK\"'", True, True),
    ("uv run python -c 'print(\"CHE\"+\"CK_OK\")'", True, True),
    ("python -m pytest -c pytest.ini tests.py", True, False),
    ("python -W ignore -c 'print(1)'", True, True),
    ("python -X dev -c 'print(1)'", True, True),
    ("python check.py -c settings.json", True, False),
    ("python check.py node -p artifact.txt", True, False),
    ("./check.sh python -c config.json", True, False),
    ("node check.js -p artifact.txt", True, False),
    ("node --require setup.js -e 'console.log(1)'", True, True),
    ("perl check.pl -e settings.json", True, False),
    ("ruby check.rb -e settings.json", True, False),
    ("php check.php -r settings.json", True, False),
    ("lua check.lua -e settings.json", True, False),
    ("perl -I lib -E 'say 1'", True, True),
    ("ruby -I lib -e 'puts 1'", True, True),
    ("php -c config.ini -r 'echo 1;'", True, True),
    ("lua -l module -e 'print(1)'", True, True),
    ("bash check.sh", True, False),
    ("bash -- check.sh -c settings.json", True, False),
    ("bash -e check.sh", True, False),
    ("bash -eux -- check.sh -c settings.json", True, False),
    ("bash -euo pipefail check.sh", True, False),
    ("bash -o errexit -o nounset check.sh", True, False),
    ("bash -e", False, True),
    ("bash -euo", False, True),
    ("bash -s check.sh", False, True),
    ("bash -n check.sh", False, True),
    ("bash -o noexec check.sh", False, True),
    ("bash -ec 'echo CHECK_OK'", False, True),
    ("python -c 'import sys; print(sys.stdin.read())' <<< CHECK_OK", False, False),
    ("python -c 'import sys; print(sys.stdin.read())' <<< \"$X\"", False, False),
    ("python check.py < /dev/null", True, False),
])
def test_static_output_cannot_satisfy_an_expected_probe(command, executable, supplied):
    assert _verification_mode(command) is executable
    assert _static_printer_supplies(command, "CHECK_OK") is supplied


@pytest.mark.parametrize("arguments", ["-c settings.json", "node -p artifact.txt"])
@pytest.mark.parametrize("passed", [True, False])
async def test_python_script_arguments_preserve_behavior_probe(tmp_path, arguments, passed):
    (tmp_path / "artifact.txt").write_text("42\n", encoding="utf-8")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nimport sys\n"
        f"assert sys.argv[1:] == {shlex.split(arguments)!r}\n"
        "Path('executed').write_text('yes')\n"
        f"assert Path('artifact.txt').read_text() == {('42' if passed else '43')!r} + '\\n'\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} check.py {arguments}"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["executed"] is True and output["ok"] is passed


@pytest.mark.parametrize("passed", [True, False])
async def test_node_script_arguments_preserve_behavior_probe(tmp_path, passed):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the JavaScript assertion probe")
    (tmp_path / "artifact.txt").write_text("42\n", encoding="utf-8")
    (tmp_path / "check.js").write_text(
        "const fs = require('node:fs');\nconst assert = require('node:assert/strict');\n"
        "assert.deepEqual(process.argv.slice(2), ['-p', 'artifact.txt']);\n"
        "fs.writeFileSync('executed', 'yes');\n"
        f"assert.equal(fs.readFileSync(process.argv[3], 'utf8'), {('42' if passed else '43')!r} + '\\n');\n"
        "console.log('CHECK_OK');\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(node)} check.js -p artifact.txt"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["executed"] is True and output["ok"] is passed


@pytest.mark.parametrize("interpreter,suffix,flag,source", [
    ("perl", "pl", "-e",
     'die "args" unless "@ARGV" eq "-e settings.json";\n'
     'open(my $input, "<", "artifact.txt") or die $!;\n'
     'my $actual = <$input>; chomp $actual;\n'
     'open(my $marker, ">", "executed") or die $!; print $marker "yes";\n'
     'die "bad artifact" unless $actual eq "VALUE";\nprint "CHECK_OK\\n";\n'),
    ("ruby", "rb", "-e",
     "raise 'args' unless ARGV == ['-e', 'settings.json']\n"
     "File.write('executed', 'yes')\n"
     "raise 'bad artifact' unless File.read('artifact.txt').strip == 'VALUE'\nputs 'CHECK_OK'\n"),
    ("php", "php", "-r",
     "<?php\nif (array_slice($argv, 1) !== ['-r', 'settings.json']) { exit(2); }\n"
     "file_put_contents('executed', 'yes');\n"
     "if (trim(file_get_contents('artifact.txt')) !== 'VALUE') { exit(1); }\n"
     'echo "CHECK_OK\\n";\n'),
    ("lua", "lua", "-e",
     'assert(arg[1] == "-e" and arg[2] == "settings.json")\n'
     'local input = assert(io.open("artifact.txt")); local actual = input:read("*a"); input:close()\n'
     'local marker = assert(io.open("executed", "w")); marker:write("yes"); marker:close()\n'
     'assert(actual == "VALUE\\n")\nprint("CHECK_OK")\n'),
])
@pytest.mark.parametrize("passed", [True, False])
async def test_other_interpreter_script_arguments_preserve_behavior_probe(
    tmp_path, interpreter, suffix, flag, source, passed,
):
    executable = shutil.which(interpreter)
    if executable is None:
        pytest.skip(f"{interpreter} is required for its assertion probe")
    (tmp_path / "artifact.txt").write_text("42\n", encoding="utf-8")
    script = f"check.{suffix}"
    (tmp_path / script).write_text(source.replace("VALUE", "42" if passed else "43"), encoding="utf-8")
    command = f"{shlex.quote(executable)} {script} {flag} settings.json"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["executed"] is True and output["ok"] is passed


@pytest.mark.parametrize("options,arguments", [
    ("", ""),
    ("--", ""),
    ("", "python -c settings.json"),
    ("--", "python -c settings.json"),
    ("-e", ""),
    ("-u", ""),
    ("-x", ""),
    ("-eu", "python -c settings.json"),
    ("-eux --", "python -c settings.json"),
    ("-o pipefail", ""),
    ("-euo pipefail", ""),
])
@pytest.mark.parametrize("passed", [True, False])
async def test_shell_file_script_preserves_assertion_probe(tmp_path, options, arguments, passed):
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("Bash is required for the shell assertion probe")
    (tmp_path / "artifact.txt").write_text("42\n", encoding="utf-8")
    (tmp_path / "check.sh").write_text(
        "test -f artifact.txt || exit 1\n"
        f"test \"$*\" = {shlex.quote(arguments)} || exit 1\n"
        "printf yes > executed\n"
        "read -r actual < artifact.txt\n"
        f"test \"$actual\" = {('42' if passed else '43')} || exit 1\n"
        "printf 'CHECK_OK\\n'\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(shell)} {options} check.sh {arguments}"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["executed"] is True and output["ok"] is passed


async def test_expected_output_can_overlap_a_real_script_argument(tmp_path):
    (tmp_path / "check.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "assert Path('artifact.txt').read_text() == sys.argv[1]\n"
        "print('correct')\n",
        encoding="utf-8",
    )
    (tmp_path / "artifact.txt").write_text("correct", encoding="utf-8")
    command = f"{shlex.quote(sys.executable)} check.py correct"
    output = await _check(tmp_path, {"command": command, "expected_output": "correct"})
    assert output["executed"] is True and output["ok"] is True


@pytest.mark.parametrize("target,options", [
    ("test_result.py", ""),
    ("test_result.py", "--junit-prefix '#suite'"),
    ("test_result.py", r"--junit-prefix \#suite"),
    ("test_result.py", "# keep this note"),
    ("test_result.py", "#don't parse this ' comment; | < > `"),
    ("#test_result.py", ""),
    ("test#result.py", ""),
])
async def test_pytest_target_output_and_quoted_hash_option_preserve_test_execution(tmp_path, target, options):
    (tmp_path / target).write_text(
        "from pathlib import Path\ndef test_result():\n"
        "    Path('executed').write_text('yes')\n"
        "    assert 6 * 7 == 42\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider {shlex.quote(target)} {options}"
    output = await _check(tmp_path, {"command": command, "expected_output": target})
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["executed"] is True and output["ok"] is True
    assert all(row["verified"] for row in output["report"]["checks"][0]["test_records"])


async def test_clustered_python_flag_cannot_authenticate_a_one_liner(tmp_path):
    command = f"{shlex.quote(sys.executable)} -bc 'print(\"CHE\"+\"CK_OK\")'"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is True
    assert output["ok"] is False
    assert output["report"]["checks"][0]["reason"] == "expected output is supplied by a static printer"


async def test_decoy_file_read_cannot_authenticate_a_one_liner(tmp_path):
    command = f"{shlex.quote(sys.executable)} -c 'x=\"open(\"; print(\"CHE\"+\"CK_OK\")'"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is True
    assert output["ok"] is False
    assert output["report"]["checks"][0]["reason"] == "expected output is supplied by a static printer"


async def test_here_string_cannot_authenticate_a_noop_program(tmp_path):
    output = await _check(
        tmp_path,
        {"command": "python -c 'import sys; print(sys.stdin.read())' <<< CHECK_OK", "expected_output": "CHECK_OK"},
    )
    assert output["executed"] is False
    assert output["ok"] is False


async def test_printer_prefix_cannot_authenticate_a_noop_program(tmp_path):
    command = f"echo CHECK_OK && {shlex.quote(sys.executable)} -c 'import sys; sys.exit(0)'"
    output = await _check(
        tmp_path,
        {"command": command, "expected_output": "CHECK_OK"},
    )
    assert output["executed"] is True
    assert output["ok"] is False
    assert output["report"]["checks"][0]["reason"] == "expected output is supplied by a static printer"


@pytest.mark.parametrize("passed", [False, True])
@pytest.mark.parametrize("wrapper", ["", "env CHECK_ENV=yes "])
async def test_echo_prefix_executes_actual_behavior_probe(tmp_path, passed, wrapper):
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('executed').write_text('yes')\n"
        f"assert {passed!r}\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = f"echo 'Starting check' && {wrapper}{shlex.quote(sys.executable)} check.py"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["executed"] is True
    assert output["ok"] is passed


@pytest.mark.parametrize("command", [
    "echo CHECK_OK", "printf CHECK_OK", "echo Starting && echo CHECK_OK",
    "echo Starting && printf CHECK_OK", "echo 'Starting && python check.py CHECK_OK'",
    "echo CHECK_OK # && python check.py", "echo CHECK_OK && echo '&&' python check.py",
    "echo CHECK_OK | cat", "echo CHECK_OK; printf CHECK_OK", "echo Starting && /bin/echo CHECK_OK",
    "echo Starting && env printf CHECK_OK", "echo Starting && command printf CHECK_OK",
    "echo Starting && /usr/bin/env CHECK_ENV=yes printf CHECK_OK",
    "echo Starting && (printf CHECK_OK)", "echo Starting && { printf CHECK_OK; }",
    "echo Starting && true; printf CHECK_OK", "echo Starting && :; printf CHECK_OK",
    "echo Starting && false || printf CHECK_OK", "echo Starting && false | printf CHECK_OK",
    "echo Starting && false & printf CHECK_OK",
    "echo Starting && true\nprintf CHECK_OK", "echo Starting && :\nprintf CHECK_OK",
    "echo Starting && false\nprintf CHECK_OK",
    "echo Starting && false # a comment\nprintf CHECK_OK",
    "exec echo CHECK_OK",
    "exec printf CHECK_OK",
    "eval echo CHECK_OK",
    "busybox echo CHECK_OK",
    "nice echo CHECK_OK",
    "xargs echo CHECK_OK",
    "/usr/bin/time echo CHECK_OK",
    "bash -c 'echo CHECK_OK'",
    "sh -c 'printf CHECK_OK'",
])
async def test_static_printing_compounds_remain_unverified(tmp_path, command):
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is False
    assert output["ok"] is False


@pytest.mark.parametrize("group", ["({command})", "{{ {command}; }}"])
async def test_grouped_behavior_probes_remain_unverified(tmp_path, group):
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('executed').write_text('yes')\n"
        "assert 6 * 7 == 42\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = "echo Starting && " + group.format(command=f"{shlex.quote(sys.executable)} check.py")
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is False and output["ok"] is False
    assert not (tmp_path / "executed").exists()


@pytest.mark.parametrize("quote", ["'", '"'])
async def test_quoted_group_characters_preserve_real_probe_execution(tmp_path, quote):
    target = "check (result) {value}.py"
    (tmp_path / target).write_text(
        "from pathlib import Path\nimport sys\n"
        "assert sys.argv[1] == 'argument (result) {value} ; | & &&'\n"
        "Path('executed').write_text('yes')\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = f"echo {quote}Starting (check) {{value}}{quote} && {shlex.quote(sys.executable)} "
    command += f"{quote}{target}{quote} {quote}argument (result) {{value}} ; | & &&{quote}"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is True and output["ok"] is True
    assert (tmp_path / "executed").read_text() == "yes"


async def test_escaped_control_characters_preserve_real_probe_execution(tmp_path):
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "assert sys.argv[1] == 'argument;|& '\n"
        "Path('executed').write_text('yes')\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = f"echo Starting && {shlex.quote(sys.executable)} check.py argument\\;\\|\\&\\ "
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is True and output["ok"] is True
    assert (tmp_path / "executed").read_text() == "yes"


@pytest.mark.parametrize("padding", [("", ""), ("", "\n"), ("", "\r\n"), (" \n\t", " \t\r\n")])
async def test_probe_padding_preserves_real_probe_execution(tmp_path, padding):
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "assert len(sys.argv) == 1\n"
        "assert 6 * 7 == 42\n"
        "Path('executed').write_text('yes')\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = padding[0] + f"{shlex.quote(sys.executable)} check.py" + padding[1]
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"})
    assert output["executed"] is True and output["ok"] is True
    assert (tmp_path / "executed").read_text() == "yes"
    assert output["report"]["checks"][0]["command"] == command


@pytest.mark.parametrize("argument", ["pytest", "unittest", "/opt/runners/pytest"])
@pytest.mark.parametrize("prefix", ["", "echo Starting && env CHECK_ENV=yes "])
async def test_script_runner_argument_preserves_assertion_probe_delivery(tmp_path, argument, prefix):
    (tmp_path / "runner.txt").write_text(argument, encoding="utf-8")
    (tmp_path / "check_runner.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "assert Path('runner.txt').read_text() == sys.argv[1]\n"
        "assert Path('result.txt').read_text() == 'correct\\n'\n"
        "Path('executed').write_text('yes')\n"
        "print('CHECK_OK')\n",
        encoding="utf-8",
    )
    command = f"{prefix}{shlex.quote(sys.executable)} check_runner.py {shlex.quote(argument)}"
    result = await OpenCollab(tmp_path, model="offline").workflow(
        "weave", {"groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
                  "check_commands": [{"command": command, "expected_output": "CHECK_OK"}],
                  "config": {"max_steps": 4, "max_repair_rounds": 0}},
        llm=FileModel(initial="correct\n"), budget=20_000, limit_mode="explicit", trace=False, timeout=20,
    )
    assert result.ok, result.reason
    assert (tmp_path / "executed").read_text() == "yes"
    assert result.output["status"] == "completed" and result.output["delivery_ok"] is True
    row = result.output["verification"]["report"]["checks"][0]
    assert row["executed"] is True and row["exit_code"] == 0 and row["matched"] is True
    assert not row["test_records"]


@pytest.mark.parametrize("target", ["test_result.py", "test result.py"])
@pytest.mark.parametrize("separator", [False, True])
@pytest.mark.parametrize("padding", [("", ""), ("", "\n"), ("", "\r\n"), (" \n\t", " \t\r\n")])
async def test_documented_quiet_pytest_verifies_actual_target(tmp_path, target, separator, padding):
    (tmp_path / target).write_text(
        "from pathlib import Path\n"
        "def test_result():\n"
        "    Path('test-executed').write_text('yes')\n"
        "    assert Path('result.txt').read_text() == 'correct\\n'\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
    command += ("-- " if separator else "") + shlex.quote(target)
    command = padding[0] + command + padding[1]
    result = await OpenCollab(tmp_path, model="offline").workflow(
        "weave", {"groups": [{"id": "file", "prompt": "write-stage writes result.txt"}],
                  "check_commands": [command], "config": {"max_steps": 4, "max_repair_rounds": 0}},
        llm=FileModel(initial="correct\n"), budget=20_000, limit_mode="explicit", trace=False, timeout=20,
    )
    assert result.ok, result.reason
    assert (tmp_path / "test-executed").read_text() == "yes"
    assert result.output["delivery_ok"] is True
    row = result.output["verification"]["report"]["checks"][0]
    assert row["command"] == command
    assert row["test_records"] and all(record["verified"] for record in row["test_records"])
    assert f"PASSED {target}::test_result" in row["output"]


async def test_quiet_pytest_preserves_cd_quotes_variables_and_selected_test(tmp_path, monkeypatch):
    directory = tmp_path / "check directory"
    directory.mkdir()
    (directory / "test result.py").write_text(
        "from pathlib import Path\n"
        "def test_selected():\n"
        "    Path('selected').write_text('yes')\n"
        "    assert 6 * 7 == 42\n"
        "def test_other():\n"
        "    raise AssertionError('selection was lost')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OC_TEST_SELECTION", "test_selected")
    command = f"cd -- 'check directory' && {shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
    command += '-k "$OC_TEST_SELECTION" -- "test result.py"'
    output = await _check(tmp_path, command)
    assert output["ok"] is True
    assert (directory / "selected").read_text() == "yes"
    row = output["report"]["checks"][0]
    assert row["executed_command"] == command.replace('-- "test result.py"', '-rA -- "test result.py"')
    assert "1 passed, 1 deselected" in row["output"]


@pytest.mark.parametrize("mode", ["passed", "failed", "empty", "help", "collect", "compound", "redirect", "suffix"])
@pytest.mark.parametrize("prefix", ["", "echo Starting && ", "echo Starting && env CHECK_ENV=yes "])
async def test_pytest_adverse_results_and_unsupported_shell_remain_unverified(tmp_path, mode, prefix):
    (tmp_path / "test_result.py").write_text(
        "from pathlib import Path\n"
        "def test_result():\n"
        "    Path('test-executed').write_text('yes')\n"
        f"    assert {mode != 'failed'!r}\n",
        encoding="utf-8",
    )
    (tmp_path / "empty").mkdir()
    command = f"{prefix}{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
    command += {"empty": "empty", "help": "--help", "collect": "test_result.py --collect-only"}.get(
        mode, "test_result.py")
    command += {"compound": " && echo CHECK_OK", "redirect": " > result.log", "suffix": "; echo CHECK_OK"}.get(mode, "")
    expected = {"passed": "passed", "empty": "no tests ran", "help": "usage", "collect": "collected", "redirect": "",
                "compound": "CHECK_OK", "suffix": "CHECK_OK"}.get(mode, "failed")
    output = await _check(tmp_path, {"command": command, "expected_output": expected or "CHECK_OK"})
    assert output["ok"] is (mode == "passed" and not prefix)
    row = output["report"]["checks"][0]
    if mode in {"help", "collect", "suffix"}:
        assert output["executed"] is False
        assert not (tmp_path / "test-executed").exists()
    else:
        assert output["executed"] is True
        if mode != "empty":
            assert (tmp_path / "test-executed").read_text() == "yes"
        if prefix or mode in {"compound", "redirect", "suffix"}:
            assert not row["test_records"]
            assert "executed_command" not in row
            assert "requires per-target test evidence" in row["reason"]


async def test_timed_out_behavior_probe_retains_execution_without_passing(tmp_path):
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nimport time\n"
        "Path('started').write_text('yes')\nprint('CHECK_OK', flush=True)\n"
        "time.sleep(5)\nPath('late').write_text('yes')\n",
        encoding="utf-8",
    )
    command = f"echo Starting && {shlex.quote(sys.executable)} check.py"
    output = await _check(tmp_path, {"command": command, "expected_output": "CHECK_OK"}, seconds=.2)
    assert (tmp_path / "started").read_text() == "yes"
    assert not (tmp_path / "late").exists()
    assert output["executed"] is True and output["ok"] is False
    assert output["report"]["checks"][0]["exit_code"] != 0


async def test_truncated_pytest_output_retains_failed_evidence(tmp_path, monkeypatch):
    from opencollab.adapters import _env_local

    monkeypatch.setattr(_env_local, "PROCESS_OUTPUT_CAPTURE_BYTES", 512)
    (tmp_path / "test_result.py").write_text(
        "from pathlib import Path\n"
        "def test_result():\n"
        "    Path('test-executed').write_text('yes')\n"
        "    print('x' * 5000)\n"
        "    assert 6 * 7 == 42\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -m pytest test_result.py -q -s -p no:cacheprovider"
    output = await _check(tmp_path, command)
    assert (tmp_path / "test-executed").read_text() == "yes"
    row = output["report"]["checks"][0]
    assert row["exit_code"] == 0
    assert "1 passed" in row["output"]
    assert row["test_records"] and all(not record["verified"] for record in row["test_records"])
    assert output["executed"] is True and output["ok"] is False
