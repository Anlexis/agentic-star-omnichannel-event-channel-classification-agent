"""AgentCore Platform v1.0"""

# The one place `config/config.yaml` is read.
#
# Under the registry the graph is constructed with that file's contents already
# loaded; standalone, the entry point has to load it itself. Both paths come
# through here so a declared value behaves the same in either deployment, and
# so there is a single answer to "what is this agent actually configured with".
#
# Every number is read through a bounded accessor rather than with `.get()`.
# A configuration value is trusted input in the sense that an operator wrote it,
# but `float("NaN")` is a value an operator can write by accident — a blank, a
# `.inf`, a string that YAML did not parse as a number — and NaN compares False
# against everything. A threshold that silently stops being a threshold is worse
# than a start-up failure, so a value outside its range refuses to compile.

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from framework.errors import ConfigError
from framework.utils.config_loader import load_agent_config

# src/services/<this file> -> parents[2] is the repository root.
_AGENT_DIR = Path(__file__).resolve().parents[2]


def runtime_config() -> Dict[str, Any]:
    """Load `config/config.yaml`, the runtime parameters the registry also passes."""
    return dict(load_agent_config(_AGENT_DIR))


def domain_config(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The `domain:` block — the pipeline's thresholds and rule tables."""
    block = (config or {}).get("domain")
    return dict(block) if isinstance(block, Mapping) else {}


def config_number(config: Mapping[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    """Read a declared number, or fail to compile.

    :raises ConfigError: when the declared value is non-numeric, non-finite, or
        outside ``[lo, hi]``.
    """
    if key not in config:
        return default
    raw = config[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ConfigError(f"config/config.yaml: domain.{key} must be a number, got {type(raw).__name__}")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"config/config.yaml: domain.{key} is not a number") from None
    if not math.isfinite(value):
        raise ConfigError(f"config/config.yaml: domain.{key} must be finite")
    if not lo <= value <= hi:
        raise ConfigError(f"config/config.yaml: domain.{key} must be within [{lo}, {hi}]")
    return value


def config_labels(config: Mapping[str, Any], key: str, default: Sequence[str]) -> List[str]:
    """Read a declared list of labels, or fail to compile.

    Labels are compared against values this template derives itself, so a
    non-string entry here is a declaration that can never match anything —
    which is the quiet failure this refuses.
    """
    if key not in config:
        return list(default)
    raw = config[key]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"config/config.yaml: domain.{key} must be a list of strings")
    return [item.strip().lower() for item in raw]
