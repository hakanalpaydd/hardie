"""Command-line interface for Hardie."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hardie.config import Config
from hardie.setup import ensure_dependencies, run_setup
from hardie.utils import Colors, colored, logger


def parse_args() -> tuple[Config, bool]:
    """Parse command line arguments. Returns (config, is_setup_mode)."""
    parser = argparse.ArgumentParser(
        description="🛡️ Hardie - Autonomously fix and harden your PRs and PR stacks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --setup                     Install dependencies
  %(prog)s --status                    Show current stack status
  %(prog)s --once --dry-run            Run once in dry-run mode
  %(prog)s --poll-interval 120         Check every 2 minutes
  %(prog)s --ai-cmd aider              Use aider instead of auggie
        """
    )

    parser.add_argument("--setup", action="store_true",
                        help="Install dependencies and exit")
    parser.add_argument("--poll-interval", type=int, default=90,
                        help="Polling interval in seconds (default: 90)")
    parser.add_argument("--max-iterations", type=int, default=3,
                        help="Max fix attempts per issue (default: 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't make changes, just show what would happen")
    parser.add_argument("--ai-cmd", default="auggie",
                        help="AI command to use (default: auggie)")
    parser.add_argument("--av-cmd", default="/Users/hakan.alpay/bin/av",
                        help="Aviator CLI path")
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd(),
                        help="Repository directory (default: current)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    parser.add_argument("--status", action="store_true",
                        help="Show status and exit")
    parser.add_argument("--update-metadata", action="store_true",
                        help="Only update PR stack metadata (descriptions) and exit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output")

    args = parser.parse_args()

    # Handle setup mode
    if args.setup:
        return Config(), True

    run_mode = "loop"
    if args.status:
        run_mode = "status"
    elif args.update_metadata:
        run_mode = "update-metadata"
    elif args.once:
        run_mode = "once"

    return Config(
        poll_interval=args.poll_interval,
        max_iterations=args.max_iterations,
        dry_run=args.dry_run,
        ai_cmd=args.ai_cmd,
        av_cmd=args.av_cmd,
        repo_dir=args.repo_dir,
        run_mode=run_mode,
        verbose=args.verbose,
    ), False


def main():
    """Main entry point."""
    config, is_setup = parse_args()

    # Handle setup mode
    if is_setup:
        run_setup()
        return

    # Auto-check dependencies on first run (silent unless issues)
    if not ensure_dependencies():
        print("\n💡 Run 'python -m hardie --setup' for installation help.\n")
        sys.exit(1)

    # Change to repo directory
    os.chdir(config.repo_dir)

    # Import here to avoid circular imports
    from hardie.core import PRStackFixer

    fixer = PRStackFixer(config)

    if not fixer.check_dependencies():
        sys.exit(1)

    if config.run_mode == "status":
        fixer.show_status()
    elif config.run_mode == "update-metadata":
        pr_numbers = fixer.get_stack_prs()
        if pr_numbers:
            logger.info(f"Updating metadata for {len(pr_numbers)} PRs: {pr_numbers}")
            fixer.update_pr_stack_metadata(pr_numbers)
            logger.info(colored("✓ PR metadata updated!", Colors.GREEN))
        else:
            logger.warning("No PRs found in stack")
    elif config.run_mode == "once":
        fixer.run_once()
    else:
        fixer.run_loop()


if __name__ == "__main__":
    main()

