"""Workstream D: rationed AI generation through the Artlist MCP.

Phases: `plan` (list + STOP) -> human `approve` -> `generate` (hard-capped).
"""
from .budget import approve, approval_hash, is_eligible  # noqa: F401
from .config import load_config  # noqa: F401
