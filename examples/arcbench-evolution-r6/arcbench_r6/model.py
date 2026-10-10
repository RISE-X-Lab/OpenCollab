"""Competition Chat compatibility through a caller-owned public LLM client."""

from __future__ import annotations


class CompetitionModel:
    """Replay reasoning without adding thinking flags or forcing tool choices.

    The supplied model endpoint must support the competition's Chat protocol.
    Both request preparation and input reservation receive the same controls.
    """

    def __init__(self, client):
        self.client = client

    @staticmethod
    def _reasoning(options):
        options = dict(options)
        # Explicit disabling still wins in the underlying request normalizer.
        options["thinking"] = True
        if options.get("thinking_params") is None:
            options["thinking_params"] = {}
        return options

    async def complete(self, messages, tools=None, **options):
        options = self._reasoning(options)
        choice = options.get("tool_choice")
        named = False
        if isinstance(choice, dict):
            kind = choice.get("type")
            if kind in {"function", "tool"} and set(choice) == {"type", "name"}:
                named = isinstance(choice["name"], str) and bool(choice["name"].strip())
            elif kind == "function" and set(choice) == {"type", "function"}:
                function = choice["function"]
                named = (
                    isinstance(function, dict)
                    and set(function) == {"name"}
                    and isinstance(function["name"], str)
                    and bool(function["name"].strip())
                )
        if choice == "required" or choice == {"type": "any"} or named:
            options["tool_choice"] = "auto"
        return await self.client.complete(messages, tools=tools, **options)

    def __getattr__(self, name):
        value = getattr(self.client, name)
        if name == "estimate_request_tokens":

            def estimate(messages, tools=None, **options):
                return value(messages, tools=tools, **self._reasoning(options))

            return estimate
        return value
