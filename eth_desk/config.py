from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mode: str
    once: bool
    interval_seconds: float
    log_path: Path
    freeze_path: Path
    state_path: Path
    heartbeat_seconds: float = 60.0
