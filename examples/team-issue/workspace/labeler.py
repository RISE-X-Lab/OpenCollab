def normalize_label(value: str) -> str:
    """Return a lowercase, hyphen-separated label."""
    return value.strip().lower().replace(" ", "-")
