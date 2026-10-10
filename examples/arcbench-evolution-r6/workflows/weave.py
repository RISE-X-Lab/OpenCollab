"""Discover the ARC-Bench example through the OC workflow loader."""

from arcbench_r6.workflow import weave as _weave

from opencollab import workflow

_spec = _weave.__workflow_spec__


@workflow(name=_spec.name, description=_spec.description, phases=_spec.phases)
async def weave(ctx, inputs):
    return await _weave(ctx, inputs)


__all__ = ["weave"]
