"""Core PRStackFixer orchestration class."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

from hardie.ai import AIAgent
from hardie.buildkite import BuildkiteFetcher
from hardie.config import Config, PRStatus
from hardie.git import GitOperations
from hardie.github import GitHubClient
from hardie.utils import Colors, colored, logger


class PRStackFixer:
    """Main class for monitoring and fixing PR stacks."""

    def __init__(self, config: Config):
        self.config = config
        self.processed_comments: set[str] = set()
        self.iteration_counts: dict[str, int] = {}

        if config.verbose:
            logger.setLevel(logging.DEBUG)

        # Initialize components
        self.github = GitHubClient(config, self.run_command, self.run_gh)
        self.git = GitOperations(config, self.run_command, self.run_av, self.run_gh)
        self.buildkite = BuildkiteFetcher(config, self.run_command)
        self.ai = AIAgent(config, self.run_command, self.run_gh, self.buildkite.fetch_log)

    def run_command(self, cmd: list[str], cwd: Optional[Path] = None,
                    capture: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run a shell command and return the result."""
        cwd = cwd or self.config.repo_dir
        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            return subprocess.run(
                cmd, capture_output=capture, text=True,
                cwd=str(cwd), timeout=timeout
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out: {' '.join(cmd)}")
            raise
        except Exception as e:
            logger.error(f"Command failed: {e}")
            raise

    def run_gh(self, args: list[str]) -> dict | list | None:
        """Run a gh command and parse JSON output."""
        cmd = ["gh"] + args
        result = self.run_command(cmd)

        if result.returncode != 0:
            logger.debug(f"gh command failed: {result.stderr}")
            return None

        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            logger.debug(f"Failed to parse JSON: {result.stdout[:200]}")
            return None

    def run_av(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        """Run an av command with configurable timeout."""
        cmd = [self.config.av_cmd] + args
        return self.run_command(cmd, timeout=timeout)

    def check_dependencies(self) -> bool:
        """Check that all required tools are available."""
        self.git.recover_from_temp_branch()

        missing = []

        result = self.run_command(["gh", "--version"])
        if result.returncode != 0:
            missing.append("gh (GitHub CLI)")

        result = self.run_command(["which", "git-branchless"])
        if result.returncode != 0:
            missing.append("git-branchless (https://github.com/arxanas/git-branchless)")

        if not Path(self.config.av_cmd).exists():
            result = self.run_command(["which", self.config.av_cmd])
            if result.returncode != 0:
                missing.append(f"av (Aviator CLI at {self.config.av_cmd})")

        if missing:
            logger.error("Missing required tools:")
            for tool in missing:
                logger.error(f"  - {tool}")
            return False

        result = self.run_command(["which", self.config.ai_cmd])
        if result.returncode != 0:
            logger.warning(f"AI command '{self.config.ai_cmd}' not found")

        bk_tools = []
        if Path(self.config.bklog_cmd).exists():
            bk_tools.append("bklog (caching + search)")
        result = self.run_command(["which", self.config.bk_cmd])
        if result.returncode == 0:
            bk_tools.append("bk CLI")

        if bk_tools:
            logger.info(f"Buildkite log tools available: {', '.join(bk_tools)}")
        else:
            logger.warning("No Buildkite CLI tools found - will try cookie-based fetch")

        return True

    # Delegate methods to components
    def get_owner_repo(self) -> Tuple[str, str]:
        return self.github.get_owner_repo()

    def get_pr_ci_status(self, pr_number: int) -> PRStatus:
        return self.github.get_pr_ci_status(pr_number)

    def get_copilot_comments(self, pr_number: int) -> list[dict]:
        return self.github.get_copilot_comments(pr_number)

    def resolve_review_thread(self, thread_id: str) -> bool:
        return self.github.resolve_review_thread(thread_id)

    def reply_to_review_thread(self, pr_number: int, thread_id: str, body: str) -> bool:
        return self.github.reply_to_review_thread(pr_number, thread_id, body)

    def request_copilot_review(self, pr_number: int) -> None:
        return self.github.request_copilot_review(pr_number)

    def update_pr_stack_metadata(self, pr_numbers: list[int]) -> None:
        return self.github.update_pr_stack_metadata(pr_numbers)

    def get_branch_for_pr(self, pr_number: int) -> Optional[str]:
        return self.github.get_branch_for_pr(pr_number)

    def get_stack_branches(self) -> list[str]:
        return self.git.get_stack_branches()

    def get_stack_prs(self) -> list[int]:
        return self.git.get_stack_prs()

    def push_stack(self, current_branch: Optional[str] = None) -> bool:
        return self.git.push_stack(current_branch)

    def commit_changes(self, issue_type: str, amend: bool = False) -> bool:
        return self.git.commit_changes(issue_type, amend)

    def build_pr_issues_context(self, pr_number: int, failed_checks: list, comments: list) -> str:
        return self.ai.build_pr_issues_context(pr_number, failed_checks, comments)

    def invoke_ai_agent(self, context: str, issue_type: str) -> Tuple[bool, dict]:
        return self.ai.invoke_ai_agent(context, issue_type)

    def has_pending_ci(self, pr_number: int) -> bool:
        """Check if PR has any pending CI checks."""
        status = self.get_pr_ci_status(pr_number)
        return len(status.ci_pending) > 0

    def wait_for_ci(self, pr_number: int, timeout: int = 600) -> bool:
        """Wait for CI to finish (pass or fail). Returns True if CI passed."""
        logger.info(f"Waiting for CI on PR #{pr_number}...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            status = self.get_pr_ci_status(pr_number)

            if len(status.ci_pending) == 0:
                if len(status.ci_failed) == 0:
                    logger.info(colored(f"PR #{pr_number} CI passed!", Colors.GREEN))
                    return True
                else:
                    logger.warning(f"PR #{pr_number} CI failed with {len(status.ci_failed)} failures")
                    return False

            pending_count = len(status.ci_pending)
            logger.info(f"PR #{pr_number}: {pending_count} checks still pending...")
            time.sleep(30)

        logger.warning(f"Timeout waiting for CI on PR #{pr_number}")
        return False

    def process_pr(self, pr_number: int, ci_only: bool = False) -> Tuple[bool, bool]:
        """Process a single PR for ALL issues in one batched AI invocation.

        Args:
            pr_number: The PR number to process
            ci_only: If True, only process CI failures (skip Copilot comments)

        Returns:
            Tuple of (made_changes: bool, comments_addressed: bool)
        """
        logger.info(f"Processing PR #{pr_number}...")
        made_changes = False

        status = self.get_pr_ci_status(pr_number)
        ci_failures = status.ci_failed if status.ci_failed else []
        copilot_comments = []

        if not ci_only:
            copilot_comments = [
                c for c in self.get_copilot_comments(pr_number)
                if c["id"] not in self.processed_comments
            ]

        if not ci_failures and not copilot_comments:
            logger.info(f"PR #{pr_number}: No issues to address")
            return (False, False)

        iter_key = f"pr_{pr_number}"
        current_iter = self.iteration_counts.get(iter_key, 0)
        if current_iter >= self.config.max_iterations:
            logger.warning(f"Max iterations ({self.config.max_iterations}) reached for PR #{pr_number}")
            return (False, False)
        self.iteration_counts[iter_key] = current_iter + 1

        issue_summary = []
        if ci_failures:
            issue_summary.append(f"{len(ci_failures)} CI failures")
        if copilot_comments:
            issue_summary.append(f"{len(copilot_comments)} comments")
        logger.info(f"PR #{pr_number}: Addressing {', '.join(issue_summary)} in ONE AI invocation")

        context = self.build_pr_issues_context(pr_number, ci_failures, copilot_comments)
        success, issues_results = self.invoke_ai_agent(context, f"PR #{pr_number} issues")

        if not success:
            logger.error(f"AI agent failed for PR #{pr_number}")
            return (False, False)

        if self.commit_changes("PR issues"):
            made_changes = True
            logger.info(colored(f"Committed fixes for PR #{pr_number}", Colors.GREEN))

        comments_addressed = False
        for comment in copilot_comments:
            comment_id = comment["id"]
            thread_id = comment.get("thread_id", "")

            if not thread_id:
                continue

            result = issues_results.get(thread_id)

            if result:
                action = result.get("action", "")
                reason = result.get("reason", "")

                if action == "FIXED":
                    logger.info(colored(f"Thread {thread_id}: FIXED", Colors.GREEN))
                    reply_body = "Automated review: This comment has been addressed with a code fix."
                    self.reply_to_review_thread(pr_number, thread_id, reply_body)
                    self.resolve_review_thread(thread_id)
                    comments_addressed = True
                elif action == "DISMISSED":
                    logger.info(colored(f"Thread {thread_id}: DISMISSED - {reason}", Colors.YELLOW))
                    reply_body = f"Automated review: This comment has been reviewed and dismissed.\n\n**Reason:** {reason}"
                    self.reply_to_review_thread(pr_number, thread_id, reply_body)
                    self.resolve_review_thread(thread_id)
                    comments_addressed = True
            else:
                if made_changes:
                    logger.info(f"Thread {thread_id}: Resolved (implicit fix)")
                    reply_body = "Automated review: This comment has been addressed."
                    self.reply_to_review_thread(pr_number, thread_id, reply_body)
                    self.resolve_review_thread(thread_id)
                    comments_addressed = True

            self.processed_comments.add(comment_id)

        if comments_addressed and not made_changes:
            logger.info(colored(f"PR #{pr_number}: All comments dismissed (no code changes)", Colors.YELLOW))

        return (made_changes, comments_addressed)

    def show_status(self) -> None:
        """Show current status of all PRs in the stack."""
        logger.info("PR Stack Status (bottom to top - processing order):")
        print()

        pr_numbers = self.get_stack_prs()
        if not pr_numbers:
            logger.warning("No PRs found in current stack")
            return

        for i, pr_number in enumerate(pr_numbers):
            status = self.get_pr_ci_status(pr_number)
            copilot_comments = self.get_copilot_comments(pr_number)

            position = "BOTTOM" if i == 0 else ("TOP" if i == len(pr_numbers) - 1 else f"#{i+1}")
            print(colored(f"PR #{pr_number} [{position}]", Colors.BLUE))

            failed = len(status.ci_failed)
            pending = len(status.ci_pending)
            passed = len(status.ci_passed)

            if failed > 0:
                print(f"  CI: {colored(f'{failed} failed', Colors.RED)}, {pending} pending, {passed} passed")
                for check in status.ci_failed:
                    print(f"      ❌ {check['name']}")
            elif pending > 0:
                print(f"  CI: {colored(f'{pending} pending', Colors.YELLOW)}, {passed} passed")
            else:
                print(f"  CI: {colored(f'✓ All {passed} passed', Colors.GREEN)}")

            comment_count = len(copilot_comments)
            if comment_count > 0:
                print(f"  Comments: {colored(f'{comment_count} from Copilot', Colors.YELLOW)}")
            else:
                print(f"  Comments: {colored('✓ None pending', Colors.GREEN)}")

            print()

    def run_once(self) -> None:
        """Run a single check and fix cycle. Priority: CI failures first, then comments."""
        logger.info("Running single check (CI first, then comments)...")

        pr_numbers = self.get_stack_prs()
        if not pr_numbers:
            logger.warning("No PRs found in stack")
            return

        logger.info(f"Found PRs in stack (bottom to top): {pr_numbers}")

        logger.info("Scanning for CI failures...")
        pr_statuses: dict[int, PRStatus] = {}
        pr_comments: dict[int, list] = {}

        for pr_number in pr_numbers:
            pr_statuses[pr_number] = self.get_pr_ci_status(pr_number)
            pr_comments[pr_number] = self.get_copilot_comments(pr_number)

        prs_with_ci_failure = [pr for pr in pr_numbers if pr_statuses[pr].ci_failed]
        prs_with_comments = [pr for pr in pr_numbers if pr_comments[pr] and not pr_statuses[pr].ci_failed]

        if prs_with_ci_failure:
            logger.info(colored(f"CI failures found: {prs_with_ci_failure}", Colors.RED))
        if prs_with_comments:
            logger.info(f"PRs with comments (CI passing): {prs_with_comments}")

        if prs_with_ci_failure:
            target_pr = prs_with_ci_failure[0]
            target_branch = self.get_branch_for_pr(target_pr)
            logger.info(colored(f"Fixing CI failure on PR #{target_pr} ({target_branch})...", Colors.YELLOW))
            made_changes, comments_addressed = self.process_pr(target_pr)

            if made_changes:
                logger.info(f"Fixed issues in PR #{target_pr}, pushing all branches...")
                self.push_stack(current_branch=target_branch)
                self.update_pr_stack_metadata(pr_numbers)
                for pr in pr_numbers:
                    self.request_copilot_review(pr)
                logger.info(colored("Fixes applied! Run again after CI completes.", Colors.GREEN))
                return

        elif prs_with_comments:
            target_pr = prs_with_comments[0]
            target_branch = self.get_branch_for_pr(target_pr)
            logger.info(colored(f"All CI passing, fixing comments on PR #{target_pr} ({target_branch})...", Colors.GREEN))
            made_changes, comments_addressed = self.process_pr(target_pr)

            if made_changes:
                logger.info(f"Fixed comments in PR #{target_pr}, pushing all branches...")
                self.push_stack(current_branch=target_branch)
                self.update_pr_stack_metadata(pr_numbers)
                for pr in pr_numbers:
                    self.request_copilot_review(pr)
                logger.info(colored("Fixes applied! Run again after CI completes.", Colors.GREEN))
                return
            elif comments_addressed:
                logger.info(colored(f"PR #{target_pr}: All comments dismissed, no push needed", Colors.GREEN))
                remaining_prs = [p for p in prs_with_comments if p != target_pr]
                if remaining_prs:
                    logger.info(f"Moving on to next PR with comments: {remaining_prs}")
                return

        logger.info("No changes needed")

    def run_loop(self) -> None:
        """Run the main monitoring loop indefinitely until killed."""
        logger.info("Starting PR Stack Fixer (continuous mode)...")
        logger.info(f"Poll interval: {self.config.poll_interval}s")
        logger.info(f"Max iterations per issue: {self.config.max_iterations}")
        logger.info(f"AI command: {self.config.ai_cmd}")
        logger.info(f"Repository: {self.config.repo_dir}")
        logger.info("Priority: CI failures first, then comments")
        logger.info("Press Ctrl+C to stop")

        if self.config.dry_run:
            logger.warning(colored("DRY RUN MODE - no changes will be made", Colors.YELLOW))

        print()

        while True:
            try:
                logger.info("=== Checking PR stack ===")

                pr_numbers = self.get_stack_prs()
                if not pr_numbers:
                    logger.warning("No PRs found in stack. Waiting...")
                    time.sleep(self.config.poll_interval)
                    continue

                logger.info(f"Found PRs (bottom to top): {pr_numbers}")

                logger.info("Phase 1: Scanning for CI failures...")
                pr_statuses: dict[int, PRStatus] = {}
                pr_comments: dict[int, list] = {}

                for pr_number in pr_numbers:
                    pr_statuses[pr_number] = self.get_pr_ci_status(pr_number)
                    pr_comments[pr_number] = self.get_copilot_comments(pr_number)

                prs_with_ci_failure = [pr for pr in pr_numbers if pr_statuses[pr].ci_failed]
                prs_with_ci_pending = [pr for pr in pr_numbers if pr_statuses[pr].ci_pending]
                prs_with_comments = [
                    pr for pr in pr_numbers
                    if pr_comments[pr] and not pr_statuses[pr].ci_failed
                ]

                if prs_with_ci_failure:
                    logger.info(colored(f"CI failures: {prs_with_ci_failure}", Colors.RED))
                if prs_with_ci_pending:
                    logger.info(f"CI pending: {prs_with_ci_pending}")
                if prs_with_comments:
                    logger.info(f"PRs with comments: {prs_with_comments}")

                made_changes = False
                comments_addressed = False

                if prs_with_ci_failure:
                    target_pr = prs_with_ci_failure[0]
                    target_branch = self.get_branch_for_pr(target_pr)
                    logger.info(colored(
                        f"Phase 2: Fixing CI failure on PR #{target_pr} ({target_branch}) (lowest failing PR)",
                        Colors.YELLOW
                    ))

                    pr_made_changes, pr_comments_addressed = self.process_pr(target_pr, ci_only=False)

                    if pr_made_changes:
                        made_changes = True
                        logger.info(f"Fixed issues in PR #{target_pr}, pushing all branches...")
                        if self.push_stack(current_branch=target_branch):
                            self.iteration_counts[f"pr_{target_pr}"] = 0
                            self.update_pr_stack_metadata(pr_numbers)
                            for pr in pr_numbers:
                                self.request_copilot_review(pr)
                            logger.info(colored("Stack pushed! Waiting 60s for CI to restart...", Colors.GREEN))
                            time.sleep(60)
                        logger.info(colored("Stack updated! Checking CI status...", Colors.GREEN))

                elif prs_with_comments:
                    target_pr = prs_with_comments[0]
                    target_branch = self.get_branch_for_pr(target_pr)
                    logger.info(colored(
                        f"Phase 3: All CI passing, fixing comments on PR #{target_pr} ({target_branch})",
                        Colors.GREEN
                    ))

                    pr_made_changes, pr_comments_addressed = self.process_pr(target_pr)

                    if pr_made_changes:
                        made_changes = True
                        logger.info(f"Fixed comments in PR #{target_pr}, pushing all branches...")
                        if self.push_stack(current_branch=target_branch):
                            self.iteration_counts[f"pr_{target_pr}"] = 0
                            self.update_pr_stack_metadata(pr_numbers)
                            for pr in pr_numbers:
                                self.request_copilot_review(pr)
                            logger.info(colored("Stack pushed! Waiting 60s for CI to restart...", Colors.GREEN))
                            time.sleep(60)
                        logger.info(colored("Stack updated! Checking CI status...", Colors.GREEN))
                    elif pr_comments_addressed:
                        comments_addressed = True
                        logger.info(colored(f"PR #{target_pr}: All comments dismissed, no push needed", Colors.GREEN))

                if not made_changes and not comments_addressed:
                    all_ci_passed = not prs_with_ci_failure
                    any_pending = bool(prs_with_ci_pending)
                    any_comments = bool(prs_with_comments)

                    if all_ci_passed and not any_pending and not any_comments:
                        logger.info(colored("🎉 All PRs passing CI with no pending comments! Stack is green.", Colors.GREEN))
                    elif any_pending:
                        logger.info("CI checks still running...")
                    elif any_comments:
                        logger.info("Unresolved comments exist but no changes were made this cycle.")
                    else:
                        logger.info("No actionable changes needed this cycle.")

                logger.info(f"Next check in {self.config.poll_interval}s...")
                time.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                logger.info("\nStopping...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                if self.config.verbose:
                    import traceback
                    traceback.print_exc()
                time.sleep(self.config.poll_interval)

