"""Load artlist_mcp.toml and derive per-shot generation parameters."""
from __future__ import annotations

import copy
import math
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).with_name("artlist_mcp.toml")
CONFIG_ENV = "ARTLIST_MCP_CONFIG"


class ConfigError(ValueError):
    pass


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Default file, overlaid with `path` (or $ARTLIST_MCP_CONFIG) if given.

    An override file only needs the keys it changes.
    """
    with open(DEFAULT_CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    path = path or os.environ.get(CONFIG_ENV)
    if path:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        with open(p, "rb") as f:
            cfg = _deep_merge(cfg, tomllib.load(f))
    return cfg


def kind_for(shot: dict) -> str:
    """'video' or 'audio' tool family for a shot."""
    if shot["type"] in ("ai_audio", "music"):
        return "audio"
    return "video"  # ai_video, and stock_video that fell back to needs_ai


def tool_cfg(cfg: dict, kind: str) -> dict:
    try:
        return cfg["tools"][kind]
    except KeyError as e:
        raise ConfigError(f"no [tools.{kind}] section in config") from e


def gen_duration(cfg: dict, kind: str, duration_s: float) -> float:
    """Shot duration rounded UP to an allowed model duration (or the largest one)."""
    choices = sorted(float(c) for c in tool_cfg(cfg, kind).get("duration_choices") or [])
    if not choices:
        return float(math.ceil(duration_s))
    for c in choices:
        if c + 1e-9 >= duration_s:
            return c
    return choices[-1]


def estimate_credits(cfg: dict, shot: dict) -> float:
    kind = kind_for(shot)
    credits = cfg.get("credits", {})
    if kind == "audio":
        return float(credits.get("audio_flat", 0))
    t = tool_cfg(cfg, kind)
    per = credits.get("per_second", {}).get(t.get("model", ""), {}).get(t.get("resolution", ""))
    if per is None:
        per = credits.get("default_per_second", 0)
    return float(per) * gen_duration(cfg, kind, float(shot["duration_s"]))


def render_args(template: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    """'{x}' alone -> native value; other strings -> str.format; other values as-is.

    Empty-string results from an unset variable (e.g. model = "") are dropped.
    """
    out: dict[str, Any] = {}
    for k, v in template.items():
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("{") and s.endswith("}") and s[1:-1].isidentifier():
                name = s[1:-1]
                if name not in variables:
                    raise ConfigError(f"unknown template variable {{{name}}} in argument {k!r}")
                val = variables[name]
            else:
                try:
                    val = v.format(**variables)
                except KeyError as e:
                    raise ConfigError(f"unknown template variable {e} in argument {k!r}") from e
            if val == "" or val is None:
                continue
            out[k] = val
        elif isinstance(v, dict):
            out[k] = render_args(v, variables)
        else:
            out[k] = v
    return out
