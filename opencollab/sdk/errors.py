"""Stable exceptions raised by the public OpenCollab SDK."""

from __future__ import annotations


class OpenCollabSDKError(RuntimeError):
    """Base class for failures introduced at the SDK boundary."""


class InvalidSDKRequestError(OpenCollabSDKError, ValueError):
    """Raised when an SDK request violates the public input contract."""


class WorkflowManifestError(OpenCollabSDKError):
    """Raised when hardened runtime evidence cannot be read after a run."""


class WorkflowRunTimeoutError(OpenCollabSDKError, TimeoutError):
    """Raised after a timed-out workflow has completed hardened cancellation."""


class WorkflowRunLifecycleError(OpenCollabSDKError):
    """Raised when workflow-owned activity cannot reach a terminal state."""


class AgentRunTimeoutError(OpenCollabSDKError, TimeoutError):
    """Raised after a timed-out agent has been revoked and boundedly stopped."""


class AgentRunLifecycleError(OpenCollabSDKError):
    """Raised when agent-owned cleanup or persistence cannot be completed."""


__all__ = [
    "AgentRunLifecycleError",
    "AgentRunTimeoutError",
    "InvalidSDKRequestError",
    "OpenCollabSDKError",
    "WorkflowManifestError",
    "WorkflowRunLifecycleError",
    "WorkflowRunTimeoutError",
]
