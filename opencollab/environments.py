"""Public execution-environment contract and container attachment helper."""

from opencollab.application.ports import EnvironmentPort as Environment
from opencollab.bootstrap.programmatic import attach_container

__all__ = ["Environment", "attach_container"]
