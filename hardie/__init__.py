"""
Hardie - A meta-agent that autonomously fixes and hardens your PRs and PR stacks.

Usage:
    python -m hardie --repo-dir /path/to/repo --ai-cmd auggie --verbose
    python -m hardie --status
    python -m hardie --setup
"""

__version__ = "0.1.0"
__author__ = "Hakan Alpay"

from hardie.config import Config, PRStatus
from hardie.core import PRStackFixer

__all__ = ["Config", "PRStatus", "PRStackFixer", "__version__"]