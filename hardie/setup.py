"""Dependency checking and installation for Hardie."""

from __future__ import annotations

import os
import subprocess
import sys


def ensure_dependencies() -> bool:
    """
    Check and install required dependencies on first run.
    Returns True if all dependencies are available, False otherwise.
    """
    # Optional Python packages (for enhanced functionality)
    optional_packages = {
        'requests': 'requests',
        'browser_cookie3': 'browser-cookie3',
    }

    missing_packages = []
    for module_name, pip_name in optional_packages.items():
        try:
            __import__(module_name)
        except ImportError:
            missing_packages.append(pip_name)

    if missing_packages:
        print(f"🔧 Installing optional Python packages: {', '.join(missing_packages)}")
        try:
            subprocess.check_call(
                [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing_packages,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("✅ Python packages installed successfully")
        except subprocess.CalledProcessError:
            print("⚠️  Could not install optional packages (Buildkite cookie-based log fetching may not work)")

    # Check required CLI tools
    required_tools = {
        'gh': {
            'name': 'GitHub CLI',
            'checks': [['gh', '--version']],
            'install': 'brew install gh && gh auth login',
            'url': 'https://cli.github.com/',
        },
        'git-branchless': {
            'name': 'git-branchless',
            # Check multiple possible locations (brew, cargo, direct)
            'checks': [
                ['git', 'branchless', '--help'],
                ['git-branchless', '--help'],
                [os.path.expanduser('~/.cargo/bin/git-branchless'), '--help'],
            ],
            'install': 'brew install git-branchless  # or: cargo install git-branchless',
            'url': 'https://github.com/arxanas/git-branchless',
        },
    }

    missing_tools = []
    for tool_id, info in required_tools.items():
        found = False
        for check_cmd in info['checks']:
            try:
                subprocess.run(
                    check_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                found = True
                break
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue
        if not found:
            missing_tools.append(info)

    if missing_tools:
        print("\n❌ Missing required tools:\n")
        for tool in missing_tools:
            print(f"  • {tool['name']}")
            print(f"    Install: {tool['install']}")
            print(f"    More info: {tool['url']}\n")
        return False

    return True


def run_setup() -> None:
    """Run interactive setup to install all dependencies."""
    print("🛡️  Hardie Setup\n")
    print("Checking dependencies...\n")

    if ensure_dependencies():
        print("\n✅ All dependencies are installed!")
        print("\nYou're ready to run Hardie:")
        print("  python -m hardie --repo-dir /path/to/repo --ai-cmd auggie --verbose")
    else:
        print("\n⚠️  Please install the missing tools above, then run setup again.")
        sys.exit(1)

