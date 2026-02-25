# Hardie 🛡️

A meta-agent that autonomously fixes and hardens your PRs and PR stacks.

> *Stop babysitting your PRs. Let them harden themselves.*

For the full story, see [HARDIE.md](./HARDIE.md).

## Quick Start

```bash
# Run on your current PR stack
python3 hardie.py --repo-dir /path/to/your/repo --ai-cmd auggie --verbose

# Check status only
python3 hardie.py --repo-dir /path/to/your/repo --status
```

## Requirements

- Python 3.8+
- `gh` - GitHub CLI (authenticated)
- `git-branchless` - For intelligent restacking ([install](https://github.com/arxanas/git-branchless))
- An AI CLI (e.g., `auggie`, `aider`, `cursor`)
- Optional: `bk` CLI for Buildkite log fetching

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--repo-dir DIR` | Repository directory | . |
| `--ai-cmd CMD` | AI CLI command to use | auggie |
| `--poll-interval SECONDS` | How often to check for issues | 90 |
| `--max-iterations N` | Max fix attempts per issue | 3 |
| `--verbose, -v` | Verbose output | false |
| `--status` | Show status and exit | - |
| `--dry-run` | Don't make changes | false |

## How It Works

1. **CI-First Priority**: Scans all PRs bottom-to-top, fixes lowest failing PR first
2. **Fetch Logs**: Gets actual build errors from Buildkite
3. **AI Fix**: Invokes your AI agent with full context
4. **Restack & Push**: Uses git-branchless for intelligent restacking
5. **Address Comments**: Fixes or dismisses Copilot/human review comments
6. **Repeat**: Waits for CI, then loops

## Current Status

⚠️ **Super Alpha** - Built for DoorDash's web-next repo but designed to be extensible.

## Files

- `hardie.py` - Main script (was `pr_stack_fixer.py`)
- `HARDIE.md` - Full documentation and motivation
- `skills/` - Skill docs for AI agents (CI fixing, etc.)

