"""Container-init configuration for DockerEnvironment."""

import pytest

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_process import ProcessResult
from opencollab.adapters.env import DockerEnvironment

_CONTAINER_ID = "a" * 64


@pytest.mark.parametrize(("init_process", "expected_count"), [(False, 0), (True, 1)])
async def test_owned_setup_configures_container_init(
    monkeypatch,
    init_process,
    expected_count,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def run_process(command, **_kwargs):
        calls.append(tuple(command))
        return ProcessResult(0, f"{_CONTAINER_ID}\n".encode(), b"")

    monkeypatch.setattr(docker_module, "run_process", run_process)
    environment = DockerEnvironment(init_process=init_process)
    await environment.setup()

    run_command = calls[0]
    assert run_command.count("--init") == expected_count
    if init_process:
        assert run_command.index("--init") < run_command.index("python:3.11-slim")


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_container_init_flag_must_be_boolean(value) -> None:
    with pytest.raises(ValueError, match="init_process must be a boolean"):
        DockerEnvironment(init_process=value)
