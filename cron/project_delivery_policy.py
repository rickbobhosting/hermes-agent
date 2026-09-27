"""Project profile cron egress policy shared by the tool and scheduler."""

from __future__ import annotations


def is_project_profile() -> bool:
    """Return whether the active Hermes home is a Project agent profile."""
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name().startswith("project-")


def project_delivery_error(value: str | None) -> str | None:
    """Permit only local storage or this profile's configured Matrix home room."""
    if not is_project_profile():
        return None
    target = (value or "local").strip().lower()
    if target == "local":
        return None
    if target != "matrix":
        return "Project cron delivery is limited to its own Matrix room or local"
    from agent.secret_scope import get_secret

    if not get_secret("MATRIX_HOME_ROOM", "").strip():
        return "Project Matrix home room is not configured"
    return None
