# Hardie 🛡️

A meta-agent that autonomously fixes and hardens your PRs and PR stacks.

> *Stop babysitting your PRs. Let them harden themselves.*

For the full story, see [HARDIE.md](./HARDIE.md).

---

## Getting Started

### Step 1: Install Prerequisites

Hardie will auto-install Python dependencies when you first run it, but you need these tools installed:

```bash
# 1. GitHub CLI (required)
brew install gh
gh auth login

# 2. git-branchless (required for PR stacking)
brew install git-branchless
# Then initialize in your repo:
cd /path/to/your/repo
git branchless init

# 3. An AI CLI (required) - pick one:
# Option A: Augment Code CLI (auggie)
# Install from: https://www.augmentcode.com/

# Option B: Aider
pip install aider-chat

# Option C: Any CLI that accepts a prompt file
```

### Step 2: Clone Hardie

```bash
git clone https://github.com/hakanalpaydd/hardie.git
cd hardie
```

### Step 3: Create Your PR Stack

In your target repo, create a stack of PRs using git-branchless:

```bash
cd /path/to/your/repo

# Create your feature branches
git checkout -b feature/part-1
# ... make changes, commit ...
git push -u origin feature/part-1
gh pr create --title "Part 1: ..." --base main

git checkout -b feature/part-2
# ... make changes, commit ...
git push -u origin feature/part-2
gh pr create --title "Part 2: ..." --base feature/part-1
```

### Step 4: Run Hardie

```bash
# From the hardie directory, point it at your repo
python3 -m hardie \
  --repo-dir /path/to/your/repo \
  --ai-cmd auggie \
  --verbose

# Or just check the current status
python3 -m hardie --repo-dir /path/to/your/repo --status

# Setup mode to verify dependencies
python3 -m hardie --setup
```

### Step 5: Walk Away

Hardie will now:
1. Monitor all PRs in your stack
2. Fix CI failures (bottom-up)
3. Address Copilot review comments
4. Restack and push automatically
5. Repeat until everything is green ✅

---

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
| `--once` | Run one iteration and exit | false |

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    HARDIE LOOP                          │
├─────────────────────────────────────────────────────────┤
│  1. Scan all PRs in stack (bottom → top)                │
│  2. Find first PR with CI failure                       │
│  3. Fetch build logs from Buildkite                     │
│  4. Invoke AI agent with full context                   │
│  5. Commit fixes, restack with git-branchless           │
│  6. Push all affected branches                          │
│  7. If all CI green → address Copilot comments          │
│  8. Wait for CI, repeat                                 │
└─────────────────────────────────────────────────────────┘
```

---

## Troubleshooting

**"gh: command not found"**
```bash
brew install gh && gh auth login
```

**"git-branchless: command not found"**
```bash
brew install git-branchless
cd /path/to/repo && git branchless init
```

**"No PRs found in stack"**
Make sure you're on a branch that has open PRs, and that git-branchless is initialized.

**AI agent not making changes**
Try running with `--verbose` to see the full prompt being sent to the AI.

---

## Current Status

⚠️ **Super Alpha** - Built for DoorDash's web-next repo but designed to be extensible.

---

## Files

- `hardie/` - Main package (Python module)
  - `cli.py` - Command-line interface
  - `core.py` - Main PRStackFixer orchestration
  - `github.py` - GitHub/Copilot API client
  - `git.py` - Git/branchless operations
  - `ai.py` - AI agent invocation
  - `buildkite.py` - Buildkite log fetching
  - `config.py` - Configuration dataclasses
  - `setup.py` - Dependency checking
  - `utils.py` - Logging utilities
- `HARDIE.md` - Full documentation and motivation
- `skills/` - Skill docs for AI agents (CI fixing patterns, etc.)

