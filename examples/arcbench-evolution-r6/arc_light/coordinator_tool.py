"""Structural OC tool implementation shared by the coordinator tools."""


class CoordinatorTool:
    default_timeout = None
    disable_outer_timeout = False

    def to_openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
