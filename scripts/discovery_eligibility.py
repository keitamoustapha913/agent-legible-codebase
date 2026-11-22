"""Eligibility of live and archived discovery observations."""
from dataclasses import dataclass


def navigation_invalid_reason(run: dict) -> str | None:
    """Use only evidence of usable navigation, including on archived summaries."""
    events = [e for e in run.get("events", []) if e.get("event_type") == "mcp_tool_call"]
    if run.get("unfinished_commands") or any(
        e.get("output_status") == "missing" and e.get("execution_status") != "failed"
        for e in events
    ):
        return "navigation_results_missing"
    if events and not any(e.get("output_status") == "available" and
                          e.get("execution_status") == "completed" and not e.get("navigation_failure")
                          for e in events):
        return "no_usable_navigation_results"
    if run.get("agent_completion") == "completed" and not run.get("confirmed_source_reads"):
        return "no_confirmed_source_read"
    return None


@dataclass(frozen=True)
class DiscoveryEligibility:
    environment: str | None
    reason: str | None
    eligible: bool


def assess_discovery_run(run: dict, *, ignored_files_present: bool = False) -> DiscoveryEligibility:
    """Combine checkout, navigation and completion evidence before scoring a run."""
    violation = run.get("discovery_violation")
    if (ignored_files_present or run.get("changed_files") or run.get("file_change_events")
            or run.get("shell_commands") or run.get("tool_calls_not_instrumented")):
        violation = violation or "Unexpected tool or checkout change during discovery"

    if violation:
        return DiscoveryEligibility("confounded", violation, False)

    reason = run.get("discovery_invalid_reason") or navigation_invalid_reason(run)
    journal = run.get("agent", {}).get("navigation_server")
    # Older archives may have confirmed reads but no server journal.
    if not reason and run.get("confirmed_source_reads") and journal is not None and journal.get("succeeded") == 0:
        reason = "mcp_server_telemetry_missing"

    if reason:
        return DiscoveryEligibility("blocked", reason, False)

    environment = run.get("environment")
    eligible = environment == "ready" and run.get("agent_completion") == "completed"
    return DiscoveryEligibility(environment, None, eligible)
