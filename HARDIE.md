# Hardie 🛡️

**Hardie is a meta-agent that autonomously fixes and hardens your PRs and PR stacks by iteratively solving CI failures, GitHub Copilot PR comments, and human PR comments until your PR or stack of PRs is in the best shape possible.**

---

## The Problem: PR Babysitting

This situation has probably happened to you before.

You create a PR to implement a feature or fix a bug. You've tested it locally, you're happy with the code, and you're ready to move on. But then you have to wait:

- Wait for CI to run and potentially fail
- Wait for reviewer comments to come in
- Wait for GitHub Copilot review comments to appear
- Fix issues, push again, wait again...

**This ends up hijacking your flow.** You can't fully context-switch to something else—you have to keep babysitting the PR until it's fully approved and ready to merge.

A lot of the time, you're just copying and pasting CI build failures and review comments directly into your coding agent and having it solve them. Most of the time you have a decent solution that works, but then there's a lot of nits, formatting issues, and minor improvements that reviewers (both human and AI) surface.

### It Gets Exponentially Worse with PR Stacks

If you've decided to create a stack of PRs to make it easier for humans to review your changes incrementally, this problem becomes exponentially worse:

- **Each PR** in the stack can have its own comments
- **Each PR** can have its own build failures
- **Each time** you fix something in the middle of the stack, you have to restack everything
- **Each time** you restack, you need to make sure the entire tree is correct

What if you could just:

1. ✅ Create the PR (or PR stack) once
2. ✅ Test it end-to-end locally
3. ✅ Walk away and context-switch to something else
4. ✅ Come back later to a hardened, polished PR ready for final review

**That's what Hardie helps you do.**

---

## "Always Have an Agent Running"

This philosophy isn't unique to Hardie. **Mitchell Hashimoto** (founder of HashiCorp, creator of Vagrant, Terraform, Consul, Vault, and more) recently shared his new rule for building software:

> *"Always have an agent running in the background doing something. If I'm coding, I want an agent planning. If they're coding, I want to be reviewing."*
>
> *He kicks off tasks before leaving the house — research, edge-case analysis, library comparisons — so work progresses while he drives or is away.*
>
> — [The Pragmatic Engineer](https://open.substack.com/pub/pragmaticengineer/p/mitchell-hashimoto)

Hardie embodies this principle: while you're focused on your next task, Hardie is in the background fixing CI failures, addressing review comments, and hardening your PRs. Your work progresses even when you're not actively thinking about it.

---

## Why PR Stacking is Worth It

Despite the maintenance overhead, stacked PRs are incredibly valuable for software development:

- **Better for reviewers**: Smaller, focused changes are easier to understand and review
- **Targeted feedback**: Reviewers can comment on exactly the part they care about
- **Incremental merging**: If later changes in the stack have disagreements, you can still merge the earlier, approved PRs
- **Cleaner git history**: Logical, atomic commits that tell a story

The problem isn't PR stacking—**the problem is managing the stack**. Making sure every single PR builds and passes checks on its own is tedious work.

Hardie removes that burden, letting you embrace stack-based development without worrying about the maintenance.

---

## How Hardie Works

Hardie operates as a continuous loop, monitoring your PR stack and taking action:

### Phase 1: CI-First Priority
Hardie scans **all PRs** in your stack from bottom to top, looking for CI failures. It fixes the lowest failing PR first, because fixing a parent PR often resolves issues in child PRs.

### Phase 2: Fix CI Failures
When a CI failure is detected, Hardie:
- Fetches the actual build logs from Buildkite
- Invokes an AI coding agent (like Augment) with full context
- The agent analyzes the errors and makes fixes
- Changes are committed and the entire stack is restacked

### Phase 3: Address Review Comments
Once all CI is green, Hardie moves to review comments:
- Fetches unresolved Copilot and human review comments
- Invokes the AI agent to either **fix** or **dismiss** (with reason) each comment
- Replies to comment threads with what action was taken
- Resolves threads automatically

### Phase 4: Restack and Push
After any changes:
- Uses `git-branchless` for intelligent, in-memory restacking
- Pushes only the branches that changed
- Updates PR metadata so the stack relationships stay intact
- Requests fresh Copilot reviews
- Waits for CI to restart, then repeats

---

## Current Status: Super Alpha 🧪

> ⚠️ **This is very early software.**

Hardie is currently built specifically for the **web-next** repository. It knows about:
- Rush monorepo commands
- Buildkite CI patterns
- The specific tooling we use (git-branchless, av CLI, etc.)

**However**, the architecture is designed to be extensible. The core loop—detect issues, invoke agent, fix, push, repeat—can be adapted to any repository with:
- Different CI systems
- Different build tools
- Different code review patterns

If you're interested in expanding Hardie to your repo, let's talk!

---

## Getting Started

```bash
# Clone Hardie
git clone https://github.com/hakanalpaydd/hardie.git
cd hardie

# Run it against your repo
python3 -m hardie \
  --repo-dir /path/to/your/repo \
  --ai-cmd auggie \
  --poll-interval 90 \
  --verbose

# Or check status first
python3 -m hardie --repo-dir /path/to/your/repo --status
```

Then walk away. Hardie will keep working until your stack is green and comment-free.

---

## Feedback & Questions

This is a new tool and we're actively iterating. Your feedback is invaluable!

🛡️ **Hardie** — *Stop babysitting your PRs. Let them harden themselves.*

