"""Public names for selecting single-agent implementations.

Base is the default single-agent profile and currently selects Single2.
``resolve_profile_name`` returns the implementation name for configuration
and result attribution.
"""

from opencollab.bootstrap.agent_profiles import BASE_PROFILE, resolve_profile_name

__all__ = ["BASE_PROFILE", "resolve_profile_name"]
