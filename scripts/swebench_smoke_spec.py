"""Lazy access to the optional SWE-bench test-spec dependency."""


def make_test_spec(instance, namespace: str, arch: str):
    """Load the SWE-bench harness only when a smoke run needs it."""
    from swebench.harness.test_spec.test_spec import make_test_spec as factory

    return factory(instance, namespace=namespace, arch=arch)
