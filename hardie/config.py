"""Configuration and data classes for Hardie."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Configuration for Hardie."""
    poll_interval: int = 90
    max_iterations: int = 3
    dry_run: bool = False
    ai_cmd: str = "auggie"
    av_cmd: str = "/Users/hakan.alpay/bin/av"
    bklog_cmd: str = os.path.expanduser("~/go/bin/bklog")
    bk_cmd: str = "bk"
    repo_dir: Path = field(default_factory=Path.cwd)
    run_mode: str = "loop"  # loop, once, status, update-metadata
    verbose: bool = False
    buildkite_org: str = "doordash"
    buildkite_pipeline: str = "web-monorepo-pipeline"


@dataclass
class PRStatus:
    """Status of a single PR."""
    number: int
    branch: str = ""
    ci_failed: list = field(default_factory=list)
    ci_pending: list = field(default_factory=list)
    ci_passed: list = field(default_factory=list)
    copilot_comments: list = field(default_factory=list)

