"""Read comparable numeric measurements from current and historical trace records."""


def numeric_metric(run: dict, field: str, *, schema_version: int = 4) -> int | None:
    if field.startswith("source_output") and (
        schema_version != 4 or run.get("source_attribution_coverage") != "complete"
    ):
        return None
    if field.startswith("source_tokens") and schema_version == 4:
        return None
    value = run.get(field)
    return value if type(value) is int else None
