"""Explicit fleet targets for the stock web dashboard.

Hermes profiles are deliberately isolated. A dashboard may opt into inspecting
or managing selected profile homes through ``dashboard.fleet_targets`` in the
dashboard home's config.yaml, but targets are never discovered by scanning
directories and browser input is never interpreted as a filesystem path.

This candidate exposes targets for inspection only. A configured ``manage`` or
``execute`` value is parsed but not granted by the dashboard until the write
and execution routes have independent coverage.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

_log = logging.getLogger(__name__)

FLEET_SELECTOR_PREFIX = "fleet:"
FLEET_ACCESS_LEVELS = ("inspect", "manage", "execute")
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class DashboardFleetTarget:
    target_id: str
    home: Path
    label: str
    zone: str
    access: str = "inspect"

    @property
    def selector(self) -> str:
        return f"{FLEET_SELECTOR_PREFIX}{self.target_id}"


def is_fleet_selector(value: object) -> bool:
    return isinstance(value, str) and value.startswith(FLEET_SELECTOR_PREFIX)


def _dashboard_root() -> Path:
    # Request scopes use a contextvar and deliberately do not mutate the
    # process environment. Reading the raw env therefore stays anchored to the
    # dashboard process's own HERMES_HOME, including named-profile dashboards.
    raw_home = os.environ.get("HERMES_HOME", "").strip()
    if raw_home:
        return Path(raw_home)

    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def _raw_target_map() -> Mapping[str, Any]:
    config_path = _dashboard_root() / "config.yaml"
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        _log.warning("Could not load dashboard fleet targets from %s: %s", config_path, exc)
        return {}
    if not isinstance(raw, Mapping):
        return {}
    dashboard = raw.get("dashboard")
    if not isinstance(dashboard, Mapping):
        return {}
    targets = dashboard.get("fleet_targets")
    if targets is None:
        return {}
    if not isinstance(targets, Mapping):
        _log.warning("dashboard.fleet_targets must be a mapping; ignoring it")
        return {}
    return targets


def load_fleet_targets() -> dict[str, DashboardFleetTarget]:
    """Return valid allowlisted targets keyed by opaque selector.

    Each registration points at one final profile home.  Entries with malformed
    IDs, non-absolute/missing homes, or no config.yaml are skipped so one stale
    registration cannot take down the dashboard.
    """
    loaded: dict[str, DashboardFleetTarget] = {}
    for raw_id, raw_spec in _raw_target_map().items():
        target_id = str(raw_id)
        if not _TARGET_ID_RE.fullmatch(target_id):
            _log.warning("Ignoring invalid dashboard fleet target id %r", raw_id)
            continue
        if not isinstance(raw_spec, Mapping):
            _log.warning("Ignoring dashboard fleet target %s: value must be a mapping", target_id)
            continue
        raw_home = raw_spec.get("home")
        if not isinstance(raw_home, str) or not raw_home.strip():
            _log.warning("Ignoring dashboard fleet target %s: home is required", target_id)
            continue
        home = Path(raw_home)
        if not home.is_absolute():
            _log.warning("Ignoring dashboard fleet target %s: home must be absolute", target_id)
            continue
        try:
            home = home.resolve(strict=True)
        except OSError as exc:
            _log.warning("Ignoring dashboard fleet target %s: %s", target_id, exc)
            continue
        config_path = home / "config.yaml"
        if not home.is_dir() or not config_path.is_file():
            _log.warning(
                "Ignoring dashboard fleet target %s: %s is not a Hermes profile home",
                target_id,
                home,
            )
            continue
        try:
            target_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            _log.warning(
                "Ignoring dashboard fleet target %s: invalid config.yaml: %s",
                target_id,
                exc,
            )
            continue
        if not isinstance(target_config, Mapping):
            _log.warning(
                "Ignoring dashboard fleet target %s: config.yaml must be a mapping",
                target_id,
            )
            continue
        label_value = raw_spec.get("label")
        zone_value = raw_spec.get("zone")
        label = str(label_value).strip() if label_value is not None else target_id
        zone = str(zone_value).strip() if zone_value is not None else ""
        access_value = raw_spec.get("access", "inspect")
        access = str(access_value).strip().lower()
        if access not in FLEET_ACCESS_LEVELS:
            _log.warning(
                "Dashboard fleet target %s has invalid access %r; defaulting to inspect",
                target_id,
                access_value,
            )
            access = "inspect"
        target = DashboardFleetTarget(
            target_id=target_id,
            home=home,
            label=label or target_id,
            zone=zone,
            access=access,
        )
        loaded[target.selector] = target
    return loaded


def resolve_fleet_target(selector: str) -> DashboardFleetTarget | None:
    if not is_fleet_selector(selector):
        return None
    return load_fleet_targets().get(selector)


def fleet_profile_dicts() -> list[dict[str, Any]]:
    """Describe registered homes without granting a generic profile write path."""
    from hermes_cli import profiles as profiles_mod

    records: list[dict[str, Any]] = []
    for selector, target in load_fleet_targets().items():
        try:
            model, provider = profiles_mod._read_config_model(target.home)
        except Exception:
            model, provider = None, None
        try:
            meta = profiles_mod.read_profile_meta(target.home)
        except Exception:
            meta = {}
        records.append({
            "name": selector, "path": str(target.home), "is_default": False,
            "model": model, "provider": provider,
            "has_env": (target.home / ".env").is_file(),
            "skill_count": 0, "gateway_running": False,
            "description": meta.get("description", "") if isinstance(meta, dict) else "",
            "description_auto": False,
            "display_name": target.label, "bot_title": "",
            "distribution_name": None, "distribution_version": None,
            "distribution_source": None, "has_alias": False,
            "zone": target.zone, "external": True,
            "access": "inspect", "read_only": True,
        })
    return records
