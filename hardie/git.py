"""Git and git-branchless operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from hardie.utils import Colors, colored, logger

if TYPE_CHECKING:
    from hardie.config import Config


class GitOperations:
    """Handles all Git and git-branchless operations."""

    def __init__(self, config: "Config", run_command_fn, run_av_fn, run_gh_fn):
        self.config = config
        self.run_command = run_command_fn
        self.run_av = run_av_fn
        self.run_gh = run_gh_fn

    def recover_from_temp_branch(self) -> bool:
        """Recover if we're on a temporary branch left by av sync/restack."""
        result = self.run_command(["git", "branch", "--show-current"])
        if result.returncode != 0:
            return False

        current_branch = result.stdout.strip()
        if not current_branch.startswith("pr-"):
            return False

        logger.warning(f"Detected temporary branch: {current_branch}")

        result = self.run_command(["git", "branch", "--list"])
        if result.returncode != 0:
            return False

        candidates = []
        for line in result.stdout.splitlines():
            branch = line.strip().lstrip("* ")
            if branch and not branch.startswith("pr-") and branch != "master":
                candidates.append(branch)

        if not candidates:
            logger.error("No candidate branches found to recover to")
            return False

        target_branch = candidates[-1]
        logger.info(f"Recovering by checking out: {target_branch}")
        
        checkout_result = self.run_command(["git", "checkout", target_branch])
        if checkout_result.returncode != 0:
            logger.error(f"Failed to checkout {target_branch}: {checkout_result.stderr}")
            return False

        delete_result = self.run_command(["git", "branch", "-D", current_branch])
        if delete_result.returncode == 0:
            logger.info(f"Cleaned up temporary branch: {current_branch}")
        else:
            logger.warning(f"Could not delete temporary branch {current_branch}")

        logger.info(colored("Recovery successful!", Colors.GREEN))
        return True

    def commit_changes(self, issue_type: str, amend: bool = True) -> bool:
        """Commit any pending changes using git commit.

        Defaults to amending the previous commit (amend=True) because:
        1. Auto-fixes are small, mechanical changes that don't need separate commits
        2. Amending creates 'abandoned commits' that git-branchless restack handles natively
        3. This matches Graphite CLI's default behavior (gt modify amends by default)
        4. Results in cleaner git history without 'fix: auto-fix' commit spam

        Set amend=False only if you specifically need a separate commit for the fix.
        """
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would commit changes", Colors.YELLOW))
            return True

        result = self.run_command(["git", "status", "--porcelain"])
        if not result.stdout.strip():
            logger.info("No changes to commit")
            return False

        self.run_command(["git", "add", "-A"])

        if amend:
            result = self.run_command(["git", "commit", "--amend", "--no-edit"])
            if result.returncode != 0:
                logger.error(f"Failed to amend commit: {result.stderr}")
                return False
            logger.info(colored("✓ Amended commit with fixes", Colors.GREEN))
        else:
            commit_msg = f"fix: address {issue_type} issues (auto-fix)"
            result = self.run_command(["git", "commit", "-m", commit_msg])
            if result.returncode != 0:
                logger.error(f"Failed to commit: {result.stderr}")
                return False
            logger.info(colored(f"Committed: {commit_msg}", Colors.GREEN))

        return True

    def restack_prs(self) -> bool:
        """Restack all PRs using av."""
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would run: av restack", Colors.YELLOW))
            return True

        logger.info("Restacking PRs...")
        result = self.run_av(["restack"])

        if result.returncode != 0:
            logger.error(f"Failed to restack: {result.stderr}")
            return False

        logger.info(colored("PRs restacked successfully", Colors.GREEN))
        return True

    def clear_stale_sync_state(self) -> None:
        """Clear stale av sync state file."""
        state_file = os.path.join(self.config.repo_dir, ".git", "av", "stack-sync-v2.state.json")
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
                logger.debug(f"Cleared stale sync state: {state_file}")
            except OSError as e:
                logger.warning(f"Could not clear stale sync state: {e}")

    def _restack_with_branchless(self) -> bool:
        """Restack using git-branchless (does in-memory rebases).

        This is the PRIMARY method for updating child branches after amending commits.
        Since commit_changes() now amends by default, this creates 'abandoned commits'
        which git-branchless restack is specifically designed to handle.

        Fallback: If this fails (e.g., new commits were added instead of amended),
        use _move_child_branches() to move the entire subtree.
        """
        logger.info("Restacking with git-branchless...")
        result = self.run_command(
            ["git-branchless", "restack", "--in-memory"],
            timeout=180
        )
        if result.returncode != 0:
            logger.warning(f"git-branchless restack --in-memory failed: {result.stderr}")
            logger.info("Trying git-branchless restack with on-disk rebase...")
            result = self.run_command(
                ["git-branchless", "restack"],
                timeout=180
            )
            if result.returncode != 0:
                logger.error(f"git-branchless restack failed: {result.stderr}")
                return False
        logger.info(colored("✓ git-branchless restack complete", Colors.GREEN))
        return True

    def _move_child_branches(self, current_branch: str, stack_branches: list[str]) -> bool:
        """Move child branches onto the current HEAD using git-branchless move.

        This is needed after adding new commits to a parent branch, because
        git-branchless restack only handles 'abandoned' commits (from --amend),
        not divergent branches from new commits.

        Uses: git-branchless move -b <next_child_branch> -d HEAD
        This moves the child branch AND all its descendants onto HEAD.
        """
        if current_branch not in stack_branches:
            logger.warning(f"Current branch {current_branch} not in stack: {stack_branches}")
            return True  # Nothing to do

        current_idx = stack_branches.index(current_branch)

        # If we're on the last branch, no children to move
        if current_idx >= len(stack_branches) - 1:
            logger.info(f"Branch {current_branch} is the last in stack - no children to move")
            return True

        # Get the immediate child branch
        next_branch = stack_branches[current_idx + 1]
        logger.info(f"Moving child branch {next_branch} (and descendants) onto HEAD...")

        # Use git-branchless move with -b (base) to move the entire subtree
        result = self.run_command(
            ["git-branchless", "move", "-b", next_branch, "-d", "HEAD"],
            timeout=180
        )

        if result.returncode != 0:
            logger.warning(f"git-branchless move failed: {result.stderr}")
            # Try with --merge flag to handle conflicts
            logger.info("Retrying with --merge flag...")
            result = self.run_command(
                ["git-branchless", "move", "-b", next_branch, "-d", "HEAD", "--merge"],
                timeout=300
            )
            if result.returncode != 0:
                logger.error(f"git-branchless move --merge failed: {result.stderr}")
                return False

        logger.info(colored(f"✓ Moved {next_branch} and descendants onto HEAD", Colors.GREEN))
        return True

    def _submit_with_branchless(self, stack_branches: list[str]) -> bool:
        """Submit (push) branches using git-branchless submit."""
        logger.info("Submitting with git-branchless...")
        result = self.run_command(
            ["git-branchless", "submit", "--forge", "branch"] + stack_branches,
            timeout=180
        )
        if result.returncode != 0:
            logger.error(f"git-branchless submit failed: {result.stderr}")
            return False
        logger.info(colored("✓ git-branchless submit complete", Colors.GREEN))
        return True

    def push_stack(self, current_branch: Optional[str] = None) -> bool:
        """Push all changes in the stack using git-branchless.

        Strategy (hybrid approach for maximum reliability):
        1. Try git-branchless restack first - handles 'abandoned commits' from --amend
           (This is the preferred path since commit_changes() now amends by default)
        2. If restack doesn't update anything, try git-branchless move for child branches
           (Handles edge cases where new commits were added instead of amended)
        3. Push all branches using git-branchless submit or fallback to git push
        """
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would push stack", Colors.YELLOW))
            return True

        if not current_branch:
            result = self.run_command(["git", "branch", "--show-current"])
            current_branch = result.stdout.strip() if result.returncode == 0 else None

        stack_branches = self.get_stack_branches()
        if not stack_branches:
            logger.error("Could not determine stack branches")
            return False

        logger.info(f"Stack has {len(stack_branches)} branches: {stack_branches}")

        # Step 1: Try restack first (handles abandoned commits from --amend)
        # This is the primary path since commit_changes() now amends by default
        if not self._restack_with_branchless():
            logger.warning("git-branchless restack failed")
            # Step 2: Fallback to move (handles new commits added to parent branch)
            if current_branch:
                if not self._move_child_branches(current_branch, stack_branches):
                    logger.error("Both restack and move failed - stack may be in inconsistent state")
                    return False

        # Re-fetch stack branches after restack/move (commits may have new hashes)
        stack_branches = self.get_stack_branches()
        if not stack_branches:
            logger.error("Could not determine stack branches after restack/move")
            return False

        # Step 3: Push all branches
        if not self._submit_with_branchless(stack_branches):
            logger.warning("git-branchless submit failed, falling back to git push...")
            all_pushed = True
            for branch in stack_branches:
                logger.info(f"Pushing branch: {branch}")
                result = self.run_command(
                    ["git", "push", "--force-with-lease", "origin", branch],
                    timeout=60
                )
                if result.returncode != 0:
                    logger.error(f"Failed to push {branch}: {result.stderr}")
                    all_pushed = False
                else:
                    logger.info(colored(f"✓ Pushed {branch}", Colors.GREEN))
            return all_pushed

        logger.info(colored("Stack pushed successfully!", Colors.GREEN))
        return True

    def _recover_from_temp_branch_if_needed(self, original_branch: Optional[str],
                                             stack_branches: list[str]) -> None:
        """Helper to recover if we're stuck on a temp branch after av operation."""
        current_result = self.run_command(["git", "branch", "--show-current"])
        current = current_result.stdout.strip() if current_result.returncode == 0 else ""

        if current.startswith("pr-"):
            logger.warning(f"Recovering from temp branch: {current}")
            target = stack_branches[-1] if stack_branches else original_branch
            if target:
                self.run_command(["git", "checkout", target])
                self.run_command(["git", "branch", "-D", current])

    def get_stack_branches(self) -> list[str]:
        """Get all branch names in the current stack using git-branchless."""
        result = self.run_command(
            ["git-branchless", "query", "--branches", "stack()"],
            timeout=30
        )
        if result.returncode != 0:
            logger.warning(f"git-branchless query failed: {result.stderr}")
            return self._get_stack_branches_av_fallback()

        branches = []
        for line in result.stdout.splitlines():
            branch = line.strip()
            if branch and branch != "master":
                branches.append(branch)

        return branches

    def _get_stack_branches_av_fallback(self) -> list[str]:
        """Fallback: Get stack branches using av tree --current."""
        result = self.run_av(["tree", "--current"])
        if result.returncode != 0:
            return []

        branches = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("│") or line.startswith("https://") or line == "master":
                continue
            if line.startswith("*"):
                parts = line[1:].strip().split()
                if parts:
                    branch = parts[0]
                    if branch != "master" and not branch.startswith("https://"):
                        branches.append(branch)

        return branches

    def get_stack_prs(self) -> list[int]:
        """Get all PR numbers in the current stack (bottom to top order)."""
        branches = self.get_stack_branches()
        if not branches:
            return []

        pr_numbers = []
        for branch in branches:
            data = self.run_gh(["pr", "view", branch, "--json", "number"])
            if data and isinstance(data, dict):
                pr_numbers.append(data["number"])
            else:
                logger.debug(f"No PR found for branch {branch}")

        return pr_numbers

    def get_current_branch(self) -> Optional[str]:
        """Get the current branch name."""
        result = self.run_command(["git", "branch", "--show-current"])
        if result.returncode == 0:
            return result.stdout.strip()
        return None

