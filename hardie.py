#!/usr/bin/env python3
"""
PR Stack Fixer - Automatically monitors and fixes CI failures and review comments
for a stack of PRs managed by Aviator CLI (av).

Usage:
    python pr_stack_fixer.py [options]

Options:
    --poll-interval SECONDS   Polling interval (default: 60)
    --max-iterations N        Max fix attempts per issue (default: 3)
    --dry-run                 Don't commit/push, just show what would happen
    --ai-cmd CMD              AI command to use (default: auggie)
    --av-cmd CMD              Aviator CLI path
    --repo-dir DIR            Repository directory (default: current)
    --once                    Run once and exit
    --status                  Show status and exit
    --verbose, -v             Verbose output
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ANSI colors
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'  # No Color

def colored(text: str, color: str) -> str:
    """Wrap text in ANSI color codes."""
    return f"{color}{text}{Colors.NC}"

@dataclass
class Config:
    """Configuration for the PR Stack Fixer."""
    poll_interval: int = 60
    max_iterations: int = 3
    dry_run: bool = False
    ai_cmd: str = "auggie"
    av_cmd: str = "/Users/hakan.alpay/bin/av"
    bklog_cmd: str = os.path.expanduser("~/go/bin/bklog")
    bk_cmd: str = "bk"
    repo_dir: Path = field(default_factory=Path.cwd)
    run_mode: str = "loop"  # loop, once, status
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

class PRStackFixer:
    """Main class for monitoring and fixing PR stacks."""

    def __init__(self, config: Config):
        self.config = config
        self.processed_comments: set[str] = set()
        self.iteration_counts: dict[str, int] = {}
        self._owner_repo_cache: Optional[Tuple[str, str]] = None

        if config.verbose:
            logger.setLevel(logging.DEBUG)

    def get_owner_repo(self) -> Tuple[str, str]:
        """Get the owner and repo from git remote URL (cached)."""
        if self._owner_repo_cache:
            return self._owner_repo_cache

        remote_result = self.run_command(["git", "remote", "get-url", "origin"])
        if remote_result.returncode != 0:
            logger.warning("Could not get git remote URL")
            return ("", "")

        # Parse owner/repo from URL (handles both HTTPS and SSH)
        remote_url = remote_result.stdout.strip()
        match = re.search(r'[:/]([^/:]+)/([^/.]+?)(?:\.git)?$', remote_url)
        if not match:
            logger.warning(f"Could not parse owner/repo from {remote_url}")
            return ("", "")

        self._owner_repo_cache = match.groups()
        return self._owner_repo_cache

    def run_command(self, cmd: list[str], cwd: Optional[Path] = None,
                    capture: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run a shell command and return the result."""
        cwd = cwd or self.config.repo_dir
        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=capture, text=True,
                timeout=timeout
            )
            return result
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out: {' '.join(cmd)}")
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

    def parse_buildkite_url(self, url: str) -> Optional[Tuple[str, str, str, str]]:
        """Parse a Buildkite URL to extract org, pipeline, build number, and job ID.

        Returns: (org, pipeline, build_number, job_id) or None if parsing fails.
        """
        if not url or 'buildkite.com' not in url:
            return None

        try:
            parsed = urlparse(url)
            path_parts = parsed.path.strip('/').split('/')

            # Expected format: /org/pipeline/builds/build_number
            # Job ID is in the fragment (after #)
            if len(path_parts) >= 4 and path_parts[2] == 'builds':
                org = path_parts[0]
                pipeline = path_parts[1]
                build_number = path_parts[3]
                job_id = parsed.fragment if parsed.fragment else None
                return (org, pipeline, build_number, job_id)
        except Exception as e:
            logger.debug(f"Failed to parse Buildkite URL {url}: {e}")

        return None

    def fetch_buildkite_log_bklog(self, org: str, pipeline: str, build: str, job_id: str) -> Optional[str]:
        """Fetch Buildkite log using bklog CLI (best - supports caching and search)."""
        if not Path(self.config.bklog_cmd).exists():
            logger.debug("bklog not found, skipping")
            return None

        try:
            # First, export to parquet for caching
            cache_dir = Path(tempfile.gettempdir()) / "bklog_cache"
            cache_dir.mkdir(exist_ok=True)
            parquet_file = cache_dir / f"{org}_{pipeline}_{build}_{job_id}.parquet"

            # Parse and cache the log
            cmd = [
                self.config.bklog_cmd, "parse",
                "-org", org,
                "-pipeline", pipeline,
                "-build", build,
                "-job", job_id,
                "-parquet", str(parquet_file)
            ]

            result = self.run_command(cmd, timeout=120)
            if result.returncode != 0:
                logger.debug(f"bklog parse failed: {result.stderr}")
                return None

            # Query for specific errors with context - try most actionable first
            patterns = [
                "Type error:",  # TypeScript type errors (most actionable)
                "Failed to compile",  # Next.js build failures
                "error\\[E",  # Rust errors
                "FAILURE:",  # Rush CI failures
            ]

            for pattern in patterns:
                cmd = [
                    self.config.bklog_cmd, "query",
                    "-file", str(parquet_file),
                    "-op", "search",
                    "-pattern", pattern,
                    "-C", "15"  # 15 lines of context
                ]

                result = self.run_command(cmd, timeout=30)
                if result.returncode == 0 and result.stdout.strip() and "Matches found:" in result.stdout:
                    return result.stdout

            # If no specific matches, try getting the tail of the log
            cmd = [
                self.config.bklog_cmd, "query",
                "-file", str(parquet_file),
                "-op", "tail",
                "-tail", "100"
            ]
            result = self.run_command(cmd, timeout=30)
            return result.stdout if result.returncode == 0 else None

        except Exception as e:
            logger.debug(f"bklog fetch failed: {e}")
            return None

    def fetch_buildkite_log_bk(self, org: str, pipeline: str, build: str, job_id: str) -> Optional[str]:
        """Fetch Buildkite log using bk CLI (simpler fallback)."""
        try:
            cmd = [
                self.config.bk_cmd, "job", "log", job_id,
                "-p", f"{org}/{pipeline}",
                "-b", build,
                "--no-timestamps"
            ]

            result = self.run_command(cmd, timeout=120)
            if result.returncode == 0 and result.stdout.strip():
                # Extract relevant parts (last 200 lines usually have the error)
                lines = result.stdout.strip().split('\n')
                return '\n'.join(lines[-200:])

            logger.debug(f"bk job log failed: {result.stderr}")
            return None

        except Exception as e:
            logger.debug(f"bk fetch failed: {e}")
            return None

    def fetch_buildkite_log_cookies(self, org: str, pipeline: str, build: str, job_id: str) -> Optional[str]:
        """Fetch Buildkite log using browser cookies (fallback when API is unavailable)."""
        try:
            import browser_cookie3
            import requests
        except ImportError:
            logger.debug("browser_cookie3 or requests not installed, skipping cookie-based fetch")
            return None

        try:
            download_url = (
                f"https://buildkite.com/organizations/{org}/pipelines/{pipeline}"
                f"/builds/{build}/jobs/{job_id}/download.txt"
            )

            # Try to get cookies from Chrome/Arc
            try:
                cookies = browser_cookie3.chrome(domain_name='.buildkite.com')
            except Exception:
                try:
                    cookies = browser_cookie3.firefox(domain_name='.buildkite.com')
                except Exception:
                    logger.debug("Could not extract browser cookies")
                    return None

            response = requests.get(download_url, cookies=cookies, timeout=60)
            if response.status_code == 200:
                lines = response.text.strip().split('\n')
                return '\n'.join(lines[-200:])

            logger.debug(f"Cookie-based fetch failed with status {response.status_code}")
            return None

        except Exception as e:
            logger.debug(f"Cookie-based fetch failed: {e}")
            return None

    def fetch_buildkite_log(self, url: str) -> Optional[str]:
        """Fetch Buildkite log with cascading fallback.

        Tries in order:
        1. bklog (best - caching and smart search)
        2. bk CLI (simpler, still works)
        3. Cookie-based HTTP fetch (when API is unavailable)
        """
        parsed = self.parse_buildkite_url(url)
        if not parsed:
            logger.debug(f"Could not parse Buildkite URL: {url}")
            return None

        org, pipeline, build, job_id = parsed
        if not job_id:
            logger.debug(f"No job ID in URL: {url}")
            return None

        logger.debug(f"Fetching Buildkite log: {org}/{pipeline} build {build} job {job_id}")

        # Try bklog first (best)
        log = self.fetch_buildkite_log_bklog(org, pipeline, build, job_id)
        if log:
            logger.debug("Successfully fetched log using bklog")
            return log

        # Try bk CLI
        log = self.fetch_buildkite_log_bk(org, pipeline, build, job_id)
        if log:
            logger.debug("Successfully fetched log using bk CLI")
            return log

        # Try cookie-based fetch
        log = self.fetch_buildkite_log_cookies(org, pipeline, build, job_id)
        if log:
            logger.debug("Successfully fetched log using cookies")
            return log

        logger.warning(f"Could not fetch Buildkite log for {url}")
        return None

    def recover_from_temp_branch(self) -> bool:
        """Recover if we're on a temporary branch left by av sync/restack.

        av creates temporary branches like 'pr-XXXXX' during restack operations.
        If the process is interrupted, we can be left on one of these orphan branches.
        This method detects and recovers from that situation.

        Returns True if recovery was needed and successful, False otherwise.
        """
        result = self.run_command(["git", "branch", "--show-current"])
        if result.returncode != 0:
            return False

        current_branch = result.stdout.strip()

        # Check if we're on a temporary av branch (format: pr-XXXXX)
        if not current_branch.startswith("pr-"):
            return False

        logger.warning(f"Detected temporary branch from interrupted av operation: {current_branch}")

        # Try to find the actual stack branches by looking at git branches
        result = self.run_command(["git", "branch", "--list"])
        if result.returncode != 0:
            return False

        # Find branches that look like they belong to a stack (not pr-XXXXX, not master)
        candidates = []
        for line in result.stdout.splitlines():
            branch = line.strip().lstrip("* ")
            if branch and not branch.startswith("pr-") and branch != "master":
                candidates.append(branch)

        if not candidates:
            logger.error("No candidate branches found to recover to")
            return False

        # Prefer branches that match our PR stack pattern (sorted to get the highest/latest)
        target_branch = candidates[-1]  # Default to last one

        logger.info(f"Recovering by checking out: {target_branch}")
        checkout_result = self.run_command(["git", "checkout", target_branch])
        if checkout_result.returncode != 0:
            logger.error(f"Failed to checkout {target_branch}: {checkout_result.stderr}")
            return False

        # Delete the temporary branch
        delete_result = self.run_command(["git", "branch", "-D", current_branch])
        if delete_result.returncode == 0:
            logger.info(f"Cleaned up temporary branch: {current_branch}")
        else:
            logger.warning(f"Could not delete temporary branch {current_branch}")

        logger.info(colored("Recovery successful!", Colors.GREEN))
        return True

    def check_dependencies(self) -> bool:
        """Check that all required tools are available."""
        # First, recover from any interrupted av operations
        self.recover_from_temp_branch()

        missing = []

        # Check gh
        result = self.run_command(["gh", "--version"])
        if result.returncode != 0:
            missing.append("gh (GitHub CLI)")

        # Check git-branchless (required for restack and submit)
        result = self.run_command(["which", "git-branchless"])
        if result.returncode != 0:
            missing.append("git-branchless (https://github.com/arxanas/git-branchless)")

        # Check av (used for tree view and PR metadata)
        if not Path(self.config.av_cmd).exists():
            result = self.run_command(["which", self.config.av_cmd])
            if result.returncode != 0:
                missing.append(f"av (Aviator CLI at {self.config.av_cmd})")

        if missing:
            logger.error("Missing required tools:")
            for tool in missing:
                logger.error(f"  - {tool}")
            return False

        # Warn about AI command
        result = self.run_command(["which", self.config.ai_cmd])
        if result.returncode != 0:
            logger.warning(f"AI command '{self.config.ai_cmd}' not found")

        # Check Buildkite tools (optional but recommended)
        bk_tools = []
        if Path(self.config.bklog_cmd).exists():
            bk_tools.append("bklog (caching + search)")
        result = self.run_command(["which", self.config.bk_cmd])
        if result.returncode == 0:
            bk_tools.append("bk CLI")

        if bk_tools:
            logger.info(f"Buildkite log tools available: {', '.join(bk_tools)}")
        else:
            logger.warning("No Buildkite CLI tools found - will try cookie-based fetch as fallback")
            logger.warning("Install bk CLI: https://github.com/buildkite/cli")
            logger.warning("Install bklog: go install github.com/buildkite/buildkite-logs/cmd/bklog@latest")

        return True

    def get_pr_ci_status(self, pr_number: int) -> PRStatus:
        """Get CI check status for a PR."""
        status = PRStatus(number=pr_number)

        checks = self.run_gh([
            "pr", "checks", str(pr_number),
            "--json", "name,state,bucket,link"
        ])

        if not checks:
            return status

        for check in checks:
            bucket = check.get("bucket", "")
            check_info = {
                "name": check.get("name", ""),
                "state": check.get("state", ""),
                "url": check.get("link", "")
            }

            if bucket == "fail":
                status.ci_failed.append(check_info)
            elif bucket == "pending":
                status.ci_pending.append(check_info)
            elif bucket == "pass":
                status.ci_passed.append(check_info)

        return status

    def get_copilot_comments(self, pr_number: int) -> list[dict]:
        """Get unresolved review threads from Copilot on a PR using GraphQL.

        Returns review threads with their resolution status so we can:
        1. Skip already-resolved threads
        2. Resolve threads after fixing/dismissing them
        """
        # Use GraphQL to get review threads with resolution status
        query = '''
        query($owner: String!, $repo: String!, $pr: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $pr) {
                    reviewThreads(first: 100) {
                        nodes {
                            id
                            isResolved
                            comments(first: 10) {
                                nodes {
                                    id
                                    body
                                    author { login }
                                    path
                                    line
                                    createdAt
                                }
                            }
                        }
                    }
                }
            }
        }
        '''

        owner, repo = self.get_owner_repo()
        if not owner or not repo:
            return []

        # Run GraphQL query
        result = self.run_command([
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"repo={repo}",
            "-F", f"pr={pr_number}"
        ])

        if result.returncode != 0:
            logger.warning(f"GraphQL query failed: {result.stderr}")
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.warning("Could not parse GraphQL response")
            return []

        threads = (data.get("data", {})
                   .get("repository", {})
                   .get("pullRequest", {})
                   .get("reviewThreads", {})
                   .get("nodes", []))

        copilot_comments = []
        for thread in threads:
            # Skip resolved threads
            if thread.get("isResolved", False):
                continue

            thread_id = thread.get("id", "")
            comments = thread.get("comments", {}).get("nodes", [])

            # Get the first (root) comment in the thread
            if not comments:
                continue

            first_comment = comments[0]
            author = first_comment.get("author", {}).get("login", "")

            # Only process Copilot comments
            if author != "copilot-pull-request-reviewer":
                continue

            copilot_comments.append({
                "thread_id": thread_id,  # GraphQL thread ID for resolving
                "id": first_comment.get("id", ""),
                "path": first_comment.get("path", ""),
                "line": first_comment.get("line"),
                "body": first_comment.get("body", ""),
                "created_at": first_comment.get("createdAt", "")
            })

        return copilot_comments

    def resolve_review_thread(self, thread_id: str) -> bool:
        """Resolve a review thread using GraphQL mutation."""
        if self.config.dry_run:
            logger.info(colored(f"[DRY RUN] Would resolve thread {thread_id}", Colors.YELLOW))
            return True

        mutation = '''
        mutation($threadId: ID!) {
            resolveReviewThread(input: {threadId: $threadId}) {
                thread { isResolved }
            }
        }
        '''

        result = self.run_command([
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-F", f"threadId={thread_id}"
        ])

        if result.returncode != 0:
            logger.warning(f"Failed to resolve thread {thread_id}: {result.stderr}")
            return False

        logger.info(colored(f"Resolved thread {thread_id}", Colors.GREEN))
        return True

    def reply_to_review_thread(self, pr_number: int, thread_id: str, body: str) -> bool:
        """Reply to a review thread using GraphQL mutation."""
        if self.config.dry_run:
            logger.info(colored(f"[DRY RUN] Would reply to thread: {body[:50]}...", Colors.YELLOW))
            return True

        # Get PR node ID first
        result = self.run_command([
            "gh", "api", "graphql",
            "-f", "query=query($owner: String!, $repo: String!, $pr: Int!) { repository(owner: $owner, name: $repo) { pullRequest(number: $pr) { id } } }",
            "-F", "owner={owner}",
            "-F", "repo={repo}",
            "-F", f"pr={pr_number}"
        ])

        # For now, use REST API which is simpler for replies
        # Reply to the thread's last comment
        mutation = '''
        mutation($threadId: ID!, $body: String!) {
            addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
                comment { id }
            }
        }
        '''

        result = self.run_command([
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-F", f"threadId={thread_id}",
            "-f", f"body={body}"
        ])

        if result.returncode != 0:
            logger.warning(f"Failed to reply to thread: {result.stderr}")
            return False

        logger.info(colored("Replied to review thread", Colors.GREEN))
        return True

    def get_pr_full_status(self, pr_number: int) -> PRStatus:
        """Get full status including CI and comments."""
        status = self.get_pr_ci_status(pr_number)
        status.copilot_comments = [
            c for c in self.get_copilot_comments(pr_number)
            if c["id"] not in self.processed_comments
        ]
        return status

    def build_pr_issues_context(
        self,
        pr_number: int,
        failed_checks: list[dict],
        comments: list[dict]
    ) -> str:
        """Build context string for AI to fix ALL issues in a PR at once.

        This batches CI failures and review comments into a single prompt,
        so the AI can address everything in one invocation.
        """
        lines = [
            "# ⚠️ SPEED RULES - READ FIRST ⚠️",
            "",
            "- DO NOT use `find` commands (too slow)",
            "- DO NOT use `github-api` tool (IP blocked - use `gh` CLI)",
            "- DO NOT run `rushx build` (takes 15+ min)",
            "- DO NOT run `rushx typecheck` or `tsc` (codebase has pre-existing errors, not useful)",
            "- DO NOT search for files - paths are provided below",
            "- If running Jest tests, use `--runInBand --forceExit` flags to prevent hanging",
            "",
            "---",
            "",
            f"# All Issues in PR #{pr_number}",
            "",
        ]

        # Add CI failures section
        if failed_checks:
            lines.extend([
                "## 🔴 CI FAILURES (fix these first!):",
                "",
            ])
            for check in failed_checks:
                lines.append(f"### CI: {check['name']} - {check['state']}")
                url = check.get('url', '')
                if url:
                    lines.append(f"Build URL: {url}")
                    logger.info(f"Fetching error log for {check['name']}...")
                    log_content = self.fetch_buildkite_log(url)
                    if log_content:
                        lines.extend([
                            "",
                            "**Error Output:**",
                            "```",
                            log_content[:4000],  # Limit per check
                            "```",
                        ])
                    else:
                        lines.append("(Could not fetch error log)")
                lines.append("")

        # Add review comments section
        if comments:
            lines.extend([
                "## 💬 REVIEW COMMENTS:",
                "",
            ])
            for i, comment in enumerate(comments, 1):
                file_path = comment.get("path", "")
                line_num = comment.get("line")
                body = comment.get("body", "")
                thread_id = comment.get("thread_id", "")

                lines.extend([
                    f"### Comment #{i}: `{file_path}` line {line_num}",
                    f"Thread ID: {thread_id}",
                    "",
                    "**Copilot says:**",
                    "```",
                    body[:1000],  # Limit per comment
                    "```",
                    "",
                ])

                # Include relevant code context
                full_path = self.config.repo_dir / file_path
                if full_path.exists() and line_num:
                    try:
                        with open(full_path) as f:
                            file_lines = f.readlines()
                        start = max(0, line_num - 10)
                        end = min(len(file_lines), line_num + 10)
                        lines.append("**Code context:**")
                        lines.append("```")
                        for j in range(start, end):
                            marker = ">>> " if j + 1 == line_num else "    "
                            lines.append(f"{marker}{j+1}: {file_lines[j].rstrip()}")
                        lines.append("```")
                        lines.append("")
                    except Exception:
                        pass

        # Get changed files
        result = self.run_gh(["pr", "diff", str(pr_number), "--name-only"])
        if result:
            lines.extend(["## Changed Files in this PR:"])
            lines.append(result if isinstance(result, str) else str(result))
            lines.append("")

        # Instructions
        lines.extend([
            "---",
            "",
            "## Instructions:",
            "",
            "**Fix ALL issues listed above in this single session.**",
            "",
            "### For CI failures:",
            "- Read the error output and fix the code",
            "- Validate with `cd services/consumer-web-next && rushx eslint` (NOT es-lint)",
            "",
            "### For each review comment, decide: FIX or DISMISS?",
            "",
            "**FIX if:** The comment is valid and improves code quality",
            "**DISMISS if:**",
            "- Overly pedantic or stylistic preference",
            "- Code is intentionally written that way",
            "- Suggestion doesn't apply or is incorrect",
            "- Would require major refactoring out of scope",
            "",
            "### Required Output Format:",
            "",
            "After addressing ALL issues, output a summary block like this:",
            "",
            "```",
            "ISSUES_SUMMARY:",
            "- CI: FIXED (brief description)",
            "- Comment #1 (thread_id): FIXED",
            "- Comment #2 (thread_id): DISMISSED:reason",
            "- Comment #3 (thread_id): FIXED",
            "```",
            "",
            "**IMPORTANT:**",
            "- Include the thread_id in parentheses for each comment",
            "- Use FIXED or DISMISSED:reason for each",
            "- Do NOT commit - just make the code changes",
        ])

        return "\n".join(lines)

    def build_comment_fix_context(self, pr_number: int, comment: dict) -> str:
        """Build context string for AI to address a review comment.

        The AI will either:
        1. Fix the issue and output: COMMENT_ACTION: FIXED
        2. Dismiss with explanation and output: COMMENT_ACTION: DISMISSED:<reason>
        """
        file_path = comment.get("path", "")
        line_num = comment.get("line")
        body = comment.get("body", "")
        thread_id = comment.get("thread_id", "")

        lines = [
            "# ⚠️ SPEED RULES - READ FIRST ⚠️",
            "",
            "- DO NOT use `find` commands (too slow)",
            "- DO NOT use `github-api` tool (IP blocked - use `gh` CLI)",
            "- DO NOT run `rushx build` (takes 15+ min)",
            "- DO NOT run `rushx typecheck` or `tsc` (codebase has pre-existing errors, not useful)",
            "- DO NOT search for files - the path and code are provided below",
            "- If running Jest tests, use `--runInBand --forceExit` flags to prevent hanging",
            "",
            "---",
            "",
            f"# Review Comment in PR #{pr_number}",
            "",
            f"## File: `{file_path}`",
            f"## Line: {line_num}",
            f"## Thread ID: {thread_id}",
            "",
            "## Comment from Copilot:",
            "```",
            body,
            "```",
            "",
        ]

        # Try to read relevant code
        full_path = self.config.repo_dir / file_path
        if full_path.exists() and line_num:
            try:
                with open(full_path) as f:
                    file_lines = f.readlines()

                start = max(0, line_num - 15)
                end = min(len(file_lines), line_num + 15)

                lines.extend(["## Relevant Code:", "```"])
                for i in range(start, end):
                    marker = ">>> " if i + 1 == line_num else "    "
                    lines.append(f"{marker}{i+1}: {file_lines[i].rstrip()}")
                lines.append("```")
            except Exception as e:
                logger.debug(f"Could not read file {file_path}: {e}")

        lines.extend([
            "",
            "## Instructions:",
            "",
            "**IMPORTANT: The comment and code are shown above. Do NOT fetch additional data.**",
            "",
            "You must decide: Is this comment worth fixing, or should it be dismissed?",
            "",
            "### If the comment is VALID and should be FIXED:",
            "1. Make the necessary code changes to address the feedback",
            "2. For validation, use ONLY fast checks:",
            "   - `rushx eslint` for lint errors (NOT es-lint)",
            "   - Do NOT run tsc or typecheck (slow and has pre-existing errors)",
            "3. After fixing, output exactly: COMMENT_ACTION: FIXED",
            "",
            "### If the comment should be DISMISSED (not worth fixing):",
            "Reasons to dismiss:",
            "- The suggestion is overly pedantic or stylistic preference",
            "- The code is intentionally written that way for a good reason",
            "- The suggestion doesn't apply or is based on incorrect assumptions",
            "- The suggestion would require major refactoring that's out of scope",
            "",
            "If dismissing, output exactly:",
            "COMMENT_ACTION: DISMISSED:<brief reason>",
            "",
            "Example: COMMENT_ACTION: DISMISSED:Intentionally using any type here for API compatibility",
            "",
            "**DO NOT commit changes - just make the code changes if fixing.**",
        ])

        return "\n".join(lines)

    def invoke_ai_agent(self, context: str, issue_type: str) -> Tuple[bool, dict]:
        """Invoke the AI agent to fix issues.

        Returns:
            Tuple of (success, issues_results):
            - success: True if AI ran successfully
            - issues_results: Dict mapping thread_id/issue_key to {"action": "FIXED"|"DISMISSED", "reason": str}
              Special keys: "CI" for CI failures, "_single" for legacy single-comment mode
        """
        logger.info(f"Invoking AI agent to fix {issue_type}...")

        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would invoke AI with context:", Colors.YELLOW))
            print(context[:500] + "..." if len(context) > 500 else context)
            return (True, {"_dry_run": {"action": "FIXED", "reason": ""}})

        # Check if AI command exists
        result = self.run_command(["which", self.config.ai_cmd])
        if result.returncode != 0:
            logger.error(f"AI command '{self.config.ai_cmd}' not found")
            return (False, {})

        # Create temp file with prompt
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(context)
            prompt_file = f.name

        # Create output file to capture AI response
        output_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False).name

        try:
            # Determine invocation based on AI command
            ai_cmd = self.config.ai_cmd

            if "auggie" in ai_cmd:
                # Auggie: use --print for non-interactive, --instruction-file for prompt
                cmd = [ai_cmd, "--print", "--instruction-file", prompt_file]
            elif "claude" in ai_cmd:
                # Claude Code: use --print for non-interactive, prompt as positional arg
                cmd = [ai_cmd, "--print", context[:8000]]
            elif "aider" in ai_cmd:
                # Aider: use --message-file
                cmd = [ai_cmd, "--message-file", prompt_file]
            else:
                # Generic: try direct prompt
                cmd = [ai_cmd, context[:4000]]

            logger.info(f"Running: {cmd[0]} {cmd[1] if len(cmd) > 1 else ''} ...")

            # Give AI agent 5 minutes to complete, capture output
            # Use tee to both show output and capture it
            with open(output_file, 'w') as out_f:
                import subprocess
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.config.repo_dir),
                    text=True
                )

                output_lines = []
                for line in proc.stdout:
                    print(line, end='')  # Show real-time output
                    output_lines.append(line)
                    out_f.write(line)

                proc.wait()
                result_code = proc.returncode
                output = ''.join(output_lines)

            if result_code != 0:
                logger.error(f"AI command failed with exit code {result_code}")
                return (False, {})

            # Parse ISSUES_SUMMARY from output (new batched format)
            issues_results = {}
            if "ISSUES_SUMMARY:" in output:
                in_summary = False
                for line in output.splitlines():
                    if "ISSUES_SUMMARY:" in line:
                        in_summary = True
                        continue
                    if in_summary:
                        line = line.strip()
                        if not line or line.startswith("```"):
                            if line.startswith("```") and in_summary:
                                break  # End of summary block
                            continue
                        if line.startswith("- "):
                            line = line[2:]
                        # Parse format: "Comment #1 (thread_id): FIXED" or "CI: FIXED"
                        if ":" in line:
                            key_part, action_part = line.rsplit(":", 1)
                            action_part = action_part.strip()
                            # Extract thread_id if present
                            if "(" in key_part and ")" in key_part:
                                thread_id = key_part.split("(")[1].split(")")[0].strip()
                            else:
                                thread_id = key_part.strip()

                            if action_part.startswith("FIXED"):
                                issues_results[thread_id] = {"action": "FIXED", "reason": ""}
                            elif action_part.startswith("DISMISSED"):
                                reason = ""
                                if ":" in action_part:
                                    reason = action_part.split(":", 1)[1].strip()
                                issues_results[thread_id] = {"action": "DISMISSED", "reason": reason}

            # Fallback: check for old COMMENT_ACTION format (single comment mode)
            if not issues_results and "COMMENT_ACTION:" in output:
                for line in output.splitlines():
                    if "COMMENT_ACTION:" in line:
                        parts = line.split("COMMENT_ACTION:", 1)[1].strip()
                        if parts.startswith("FIXED"):
                            issues_results["_single"] = {"action": "FIXED", "reason": ""}
                        elif parts.startswith("DISMISSED"):
                            reason = parts.split(":", 1)[1].strip() if ":" in parts else ""
                            issues_results["_single"] = {"action": "DISMISSED", "reason": reason}
                        break

            return (True, issues_results)

        except Exception as e:
            logger.error(f"Error invoking AI agent: {e}")
            return (False, {})
        finally:
            os.unlink(prompt_file)
            try:
                os.unlink(output_file)
            except:
                pass

    def commit_changes(self, issue_type: str, amend: bool = False) -> bool:
        """Commit any pending changes using git commit (fast).

        Uses git commit instead of av commit for speed (~1s vs ~20s).
        Restacking is handled later by av sync --push=yes before pushing.
        """
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would commit changes", Colors.YELLOW))
            return True

        # Check for changes
        result = self.run_command(["git", "status", "--porcelain"])
        if not result.stdout.strip():
            logger.info("No changes to commit")
            return False

        # Stage changes
        self.run_command(["git", "add", "-A"])

        if amend:
            # Amend to existing commit
            result = self.run_command(["git", "commit", "--amend", "--no-edit"])
            if result.returncode != 0:
                logger.error(f"Failed to amend commit: {result.stderr}")
                return False
            logger.info(colored("Amended commit", Colors.GREEN))
        else:
            # New commit
            commit_msg = f"fix: address {issue_type} issues (auto-fix)"
            result = self.run_command(["git", "commit", "-m", commit_msg])
            if result.returncode != 0:
                logger.error(f"Failed to commit: {result.stderr}")
                return False
            logger.info(colored(f"Committed: {commit_msg}", Colors.GREEN))

        return True

    def restack_prs(self) -> bool:
        """Restack all PRs using av.

        Note: av commit already runs av restack automatically, so this is mainly
        for cases where we need to restack without committing.
        """
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
        """Clear stale av sync state file that can cause incorrect rebasing.

        The stack-sync-v2.state.json file can get stale if a previous sync was
        interrupted, causing av to use outdated parent branch information.
        Deleting it allows av sync to use the correct metadata from av.db.
        """
        state_file = os.path.join(self.config.repo_dir, ".git", "av", "stack-sync-v2.state.json")
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
                logger.debug(f"Cleared stale sync state: {state_file}")
            except OSError as e:
                logger.warning(f"Could not clear stale sync state: {e}")

    def _restack_with_branchless(self) -> bool:
        """Restack using git-branchless (does in-memory rebases)."""
        logger.info("Restacking with git-branchless...")
        # Use --in-memory for faster, safer rebases that don't touch working directory
        result = self.run_command(
            ["git-branchless", "restack", "--in-memory"],
            timeout=180
        )
        if result.returncode != 0:
            logger.warning(f"git-branchless restack --in-memory failed: {result.stderr}")
            # Fall back to on-disk rebase
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

    def _submit_with_branchless(self, stack_branches: list[str]) -> bool:
        """Submit (push) branches using git-branchless submit.

        git-branchless submit intelligently only pushes branches that have changed.
        """
        logger.info("Submitting with git-branchless...")
        # Submit all branches in the stack
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

        Strategy:
        1. If we changed a branch that's NOT the last in the stack, we MUST restack
           to update all branches above it with the new parent commit
        2. Use git-branchless restack (does in-memory rebases)
        3. Use git-branchless submit to push (only pushes changed branches)

        Args:
            current_branch: The branch that was just modified. If None, assumes
                           the current HEAD branch.
        """
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would push stack", Colors.YELLOW))
            return True

        # Get current branch if not provided
        if not current_branch:
            result = self.run_command(["git", "branch", "--show-current"])
            current_branch = result.stdout.strip() if result.returncode == 0 else None

        # Get stack branches BEFORE any operations
        stack_branches = self.get_stack_branches()
        if not stack_branches:
            logger.error("Could not determine stack branches")
            return False

        logger.info(f"Stack has {len(stack_branches)} branches: {stack_branches}")

        # Determine if we need to restack
        # We need to restack if the modified branch is NOT the last one in the stack
        is_last_branch = current_branch and stack_branches and current_branch == stack_branches[-1]

        if is_last_branch:
            logger.info(f"Branch {current_branch} is the last in stack - no restack needed")
        else:
            logger.info(f"Branch {current_branch} is not the last in stack - must restack to update children")
            if not self._restack_with_branchless():
                return False

        # Submit using git-branchless (intelligently pushes only changed branches)
        if not self._submit_with_branchless(stack_branches):
            # Fall back to manual git push if submit fails
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
        """Get all branch names in the current stack using git-branchless.

        Uses `git-branchless query --branches 'stack()'` which is fast and
        returns clean branch names in bottom-to-top order.
        """
        result = self.run_command(
            ["git-branchless", "query", "--branches", "stack()"],
            timeout=30
        )
        if result.returncode != 0:
            logger.warning(f"git-branchless query failed: {result.stderr}")
            # Fall back to av tree if git-branchless fails
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

    def get_branch_for_pr(self, pr_number: int) -> Optional[str]:
        """Get the branch name for a PR number."""
        data = self.run_gh(["pr", "view", str(pr_number), "--json", "headRefName"])
        if data and isinstance(data, dict):
            return data.get("headRefName")
        return None

    def request_copilot_review(self, pr_number: int) -> None:
        """Request a new review from Copilot."""
        if self.config.dry_run:
            logger.info(colored(f"[DRY RUN] Would request Copilot review for #{pr_number}", Colors.YELLOW))
            return

        logger.info(f"Requesting Copilot review for PR #{pr_number}...")
        # Copilot auto-reviews on push, but we can try to explicitly request
        self.run_gh([
            "pr", "edit", str(pr_number),
            "--add-reviewer", "copilot-pull-request-reviewer"
        ])

    def generate_stack_table(self, pr_numbers: list[int], current_pr: int,
                             parent_pr: Optional[int] = None) -> str:
        """Generate the av-style stack visualization table for a PR description.

        Args:
            pr_numbers: All PRs in the stack (bottom to top order)
            current_pr: The PR this table is for (will show arrow)
            parent_pr: The immediate parent PR (for "Depends on" text)

        Returns:
            HTML string with the stack table
        """
        # Build PR list (top to bottom for display)
        pr_list_reversed = list(reversed(pr_numbers))

        lines = []
        for pr in pr_list_reversed:
            if pr == current_pr:
                lines.append(f"* ➡️ **#{pr}**")
            else:
                lines.append(f"* **#{pr}**")
        lines.append("* `master`")

        pr_list_md = "\n".join(lines)

        # Create depends-on text if there's a parent
        depends_text = ""
        if parent_pr:
            depends_text = f"<b>Depends on #{parent_pr}.</b> "

        return f"""<!-- av pr stack begin -->
<table><tr><td><details><summary>{depends_text}This PR is part of a stack created with <a href="https://github.com/aviator-co/av">Aviator</a>.</summary>

{pr_list_md}
</details></td></tr></table>
<!-- av pr stack end -->"""

    def _get_branch_head_sha(self, branch_name: str) -> str:
        """Get the current HEAD commit SHA for a branch."""
        result = self.run_command(
            ["git", "rev-parse", f"origin/{branch_name}"],
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # Try without origin/ prefix
        result = self.run_command(
            ["git", "rev-parse", branch_name],
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def generate_metadata_json(self, pr_number: int, pr_numbers: list[int],
                               branch_name: str) -> str:
        """Generate the av pr metadata JSON block for a PR description.

        Args:
            pr_number: Current PR number
            pr_numbers: All PRs in stack (bottom to top)
            branch_name: Branch name for this PR

        Returns:
            HTML comment string with metadata JSON
        """
        # Find parent info
        idx = pr_numbers.index(pr_number) if pr_number in pr_numbers else -1

        if idx == 0:
            # Bottom of stack - parent is master
            parent = "master"
            parent_head = self._get_branch_head_sha("master")
            parent_pull = None
        elif idx > 0:
            # Get parent PR and branch info
            parent_pr = pr_numbers[idx - 1]
            # Get parent branch name from gh - run_gh returns parsed JSON directly
            data = self.run_gh(["pr", "view", str(parent_pr), "--json", "headRefName"])
            if data:
                parent = data.get("headRefName", "master")
            else:
                parent = "master"
            # Get the actual commit SHA of the parent branch
            parent_head = self._get_branch_head_sha(parent)
            parent_pull = parent_pr
        else:
            parent = "master"
            parent_head = self._get_branch_head_sha("master")
            parent_pull = None

        metadata = {
            "parent": parent,
            "parentHead": parent_head,
            "trunk": "master"
        }
        if parent_pull:
            metadata["parentPull"] = parent_pull

        metadata_json = json.dumps(metadata)

        return f"""<!-- av pr metadata
This information is embedded by the av CLI when creating PRs to track the status of stacks when using Aviator. Please do not delete or edit this section of the PR.
```
{metadata_json}
```
-->"""

    def update_pr_stack_metadata(self, pr_numbers: list[int]) -> None:
        """Update all PRs in the stack to have proper stack metadata.

        Ensures each PR has:
        1. Stack table at the top (showing all PRs with arrow on current)
        2. Metadata JSON at the bottom (for Aviator merge queue)

        This is idempotent - it will only add/update if missing or different.
        """
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would update PR stack metadata", Colors.YELLOW))
            return

        if not pr_numbers:
            return

        logger.info(f"Updating stack metadata for {len(pr_numbers)} PRs...")

        for i, pr_number in enumerate(pr_numbers):
            # Get current PR body - run_gh returns parsed JSON directly
            data = self.run_gh(["pr", "view", str(pr_number), "--json", "body,headRefName"])
            if not data:
                logger.warning(f"Failed to get PR #{pr_number} body")
                continue

            current_body = data.get("body", "") or ""
            branch_name = data.get("headRefName", "")

            # Find parent PR
            parent_pr = pr_numbers[i - 1] if i > 0 else None

            # Generate new metadata
            new_stack_table = self.generate_stack_table(pr_numbers, pr_number, parent_pr)
            new_metadata = self.generate_metadata_json(pr_number, pr_numbers, branch_name)

            # Check if we need to update
            has_stack_table = "<!-- av pr stack begin -->" in current_body
            has_metadata = "<!-- av pr metadata" in current_body

            # Build new body
            new_body = current_body

            # Update or add stack table at the top
            if has_stack_table:
                # Replace existing
                new_body = re.sub(
                    r'<!-- av pr stack begin -->.*?<!-- av pr stack end -->',
                    new_stack_table,
                    new_body,
                    flags=re.DOTALL
                )
            else:
                # Add at the top
                new_body = new_stack_table + "\n\n" + new_body

            # Update or add metadata at the bottom
            if has_metadata:
                # Replace existing
                new_body = re.sub(
                    r'<!-- av pr metadata.*?-->',
                    new_metadata,
                    new_body,
                    flags=re.DOTALL
                )
            else:
                # Add at the bottom
                new_body = new_body.rstrip() + "\n\n" + new_metadata

            # Only update if changed
            if new_body != current_body:
                logger.info(f"Updating metadata for PR #{pr_number}...")
                # Use gh api to update PR body
                owner, repo = self.get_owner_repo()
                result = self.run_command([
                    "gh", "api",
                    f"repos/{owner}/{repo}/pulls/{pr_number}",
                    "-X", "PATCH",
                    "-f", f"body={new_body}"
                ], timeout=30)

                if result.returncode == 0:
                    logger.info(colored(f"✓ Updated PR #{pr_number} metadata", Colors.GREEN))
                else:
                    logger.warning(f"Failed to update PR #{pr_number}: {result.stderr}")
            else:
                logger.debug(f"PR #{pr_number} metadata already up to date")

    def get_stack_prs(self) -> list[int]:
        """Get all PR numbers in the current stack (bottom to top order).

        Uses git-branchless to get branches, then gh to get PR numbers.
        This is faster than parsing av tree output.
        """
        branches = self.get_stack_branches()
        if not branches:
            logger.warning("No branches found in stack")
            return []

        # Get PR number for each branch using gh
        pr_numbers = []
        for branch in branches:
            data = self.run_gh(["pr", "view", branch, "--json", "number"])
            if data and isinstance(data, dict):
                pr_num = data.get("number")
                if pr_num and pr_num not in pr_numbers:
                    pr_numbers.append(pr_num)
            else:
                logger.debug(f"No PR found for branch {branch}")

        # git-branchless returns bottom-to-top order already
        return pr_numbers

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
                # CI finished
                if len(status.ci_failed) == 0:
                    logger.info(colored(f"PR #{pr_number} CI passed!", Colors.GREEN))
                    return True
                else:
                    logger.warning(f"PR #{pr_number} CI failed with {len(status.ci_failed)} failures")
                    return False

            # Still pending, wait and retry
            pending_count = len(status.ci_pending)
            logger.info(f"PR #{pr_number}: {pending_count} checks still pending...")
            time.sleep(30)  # Check every 30 seconds

        logger.warning(f"Timeout waiting for CI on PR #{pr_number}")
        return False

    def process_pr(self, pr_number: int, ci_only: bool = False) -> Tuple[bool, bool]:
        """Process a single PR for ALL issues in one batched AI invocation.

        Args:
            pr_number: The PR number to process
            ci_only: If True, only process CI failures (skip Copilot comments)

        Returns:
            Tuple of (made_changes: bool, comments_addressed: bool)
            - made_changes: True if code changes were made (requires push)
            - comments_addressed: True if comments were handled (fixed or dismissed)
        """
        logger.info(f"Processing PR #{pr_number}...")
        made_changes = False

        status = self.get_pr_ci_status(pr_number)

        # Collect all issues for this PR
        ci_failures = status.ci_failed if status.ci_failed else []
        copilot_comments = []

        if not ci_only:
            copilot_comments = [
                c for c in self.get_copilot_comments(pr_number)
                if c["id"] not in self.processed_comments
            ]

        # If no issues, nothing to do
        if not ci_failures and not copilot_comments:
            logger.info(f"PR #{pr_number}: No issues to address")
            return (False, False)

        # Check iteration limits
        iter_key = f"pr_{pr_number}"
        current_iter = self.iteration_counts.get(iter_key, 0)
        if current_iter >= self.config.max_iterations:
            logger.warning(f"Max iterations ({self.config.max_iterations}) reached for PR #{pr_number}")
            return (False, False)
        self.iteration_counts[iter_key] = current_iter + 1

        # Log what we're processing
        issue_summary = []
        if ci_failures:
            issue_summary.append(f"{len(ci_failures)} CI failures")
        if copilot_comments:
            issue_summary.append(f"{len(copilot_comments)} comments")
        logger.info(f"PR #{pr_number}: Addressing {', '.join(issue_summary)} in ONE AI invocation")

        # Build batched context with ALL issues
        context = self.build_pr_issues_context(pr_number, ci_failures, copilot_comments)

        # Single AI invocation for all issues
        success, issues_results = self.invoke_ai_agent(context, f"PR #{pr_number} issues")

        if not success:
            logger.error(f"AI agent failed for PR #{pr_number}")
            return (False, False)

        # Check if any code changes were made
        if self.commit_changes("PR issues"):
            made_changes = True
            logger.info(colored(f"Committed fixes for PR #{pr_number}", Colors.GREEN))

        # Process the results and resolve threads
        # Track if we addressed comments (either fixed or dismissed)
        comments_addressed = False

        for comment in copilot_comments:
            comment_id = comment["id"]
            thread_id = comment.get("thread_id", "")

            if not thread_id:
                continue

            # Check if this thread was addressed in the AI output
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
                # AI didn't explicitly mention this thread - resolve anyway if we made changes
                if made_changes:
                    logger.info(f"Thread {thread_id}: Resolved (implicit fix)")
                    reply_body = "Automated review: This comment has been addressed."
                    self.reply_to_review_thread(pr_number, thread_id, reply_body)
                    self.resolve_review_thread(thread_id)
                    comments_addressed = True

            self.processed_comments.add(comment_id)

        # If all comments were dismissed (no code changes), log it
        if comments_addressed and not made_changes:
            logger.info(colored(f"PR #{pr_number}: All comments dismissed (no code changes)", Colors.YELLOW))

        # Return made_changes for push decisions, but comments_addressed indicates we did work
        return (made_changes, comments_addressed)

    def show_status(self) -> None:
        """Show current status of all PRs in the stack."""
        logger.info("PR Stack Status (bottom to top - processing order):")
        print()

        pr_numbers = self.get_stack_prs()  # Already bottom-to-top
        if not pr_numbers:
            logger.warning("No PRs found in current stack")
            return

        for i, pr_number in enumerate(pr_numbers):
            status = self.get_pr_ci_status(pr_number)
            copilot_comments = self.get_copilot_comments(pr_number)

            position = "BOTTOM" if i == 0 else ("TOP" if i == len(pr_numbers) - 1 else f"#{i+1}")
            print(colored(f"PR #{pr_number} [{position}]", Colors.BLUE))

            # CI Status
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

            # Comments
            comment_count = len(copilot_comments)
            if comment_count > 0:
                print(f"  Comments: {colored(f'{comment_count} from Copilot', Colors.YELLOW)}")
            else:
                print(f"  Comments: {colored('✓ None pending', Colors.GREEN)}")

            print()

    def run_once(self) -> None:
        """Run a single check and fix cycle.

        Priority: CI failures first, then comments.
        """
        logger.info("Running single check (CI first, then comments)...")

        pr_numbers = self.get_stack_prs()  # Already in bottom-to-top order
        if not pr_numbers:
            logger.warning("No PRs found in stack")
            return

        logger.info(f"Found PRs in stack (bottom to top): {pr_numbers}")

        # PHASE 1: Scan ALL PRs for CI status first
        logger.info("Scanning for CI failures...")
        pr_statuses: dict[int, PRStatus] = {}
        pr_comments: dict[int, list] = {}

        for pr_number in pr_numbers:
            pr_statuses[pr_number] = self.get_pr_ci_status(pr_number)
            pr_comments[pr_number] = self.get_copilot_comments(pr_number)

        # Find PRs with CI failures (bottom to top)
        prs_with_ci_failure = [pr for pr in pr_numbers if pr_statuses[pr].ci_failed]
        prs_with_comments = [pr for pr in pr_numbers if pr_comments[pr] and not pr_statuses[pr].ci_failed]

        if prs_with_ci_failure:
            logger.info(colored(f"CI failures found: {prs_with_ci_failure}", Colors.RED))
        if prs_with_comments:
            logger.info(f"PRs with comments (CI passing): {prs_with_comments}")

        # PHASE 2: Process CI failures first
        if prs_with_ci_failure:
            target_pr = prs_with_ci_failure[0]  # First (bottom-most) failing PR
            target_branch = self.get_branch_for_pr(target_pr)
            logger.info(colored(f"Fixing CI failure on PR #{target_pr} ({target_branch})...", Colors.YELLOW))
            made_changes, comments_addressed = self.process_pr(target_pr)

            if made_changes:
                logger.info(f"Fixed issues in PR #{target_pr}, pushing all branches...")
                self.push_stack(current_branch=target_branch)
                self.update_pr_stack_metadata(pr_numbers)  # Ensure stack metadata is preserved
                for pr in pr_numbers:
                    self.request_copilot_review(pr)
                logger.info(colored("Fixes applied! Run again after CI completes.", Colors.GREEN))
                return

        # PHASE 3: Only if no CI failures, process comments
        elif prs_with_comments:
            target_pr = prs_with_comments[0]
            target_branch = self.get_branch_for_pr(target_pr)
            logger.info(colored(f"All CI passing, fixing comments on PR #{target_pr} ({target_branch})...", Colors.GREEN))
            made_changes, comments_addressed = self.process_pr(target_pr)

            if made_changes:
                logger.info(f"Fixed comments in PR #{target_pr}, pushing all branches...")
                self.push_stack(current_branch=target_branch)
                self.update_pr_stack_metadata(pr_numbers)  # Ensure stack metadata is preserved
                for pr in pr_numbers:
                    self.request_copilot_review(pr)
                logger.info(colored("Fixes applied! Run again after CI completes.", Colors.GREEN))
                return
            elif comments_addressed:
                # All comments were dismissed (no code changes needed)
                # The PR is now "clean" - move on to check for more PRs
                logger.info(colored(f"PR #{target_pr}: All comments dismissed, no push needed", Colors.GREEN))
                # Check if there are more PRs with comments to process
                remaining_prs = [p for p in prs_with_comments if p != target_pr]
                if remaining_prs:
                    logger.info(f"Moving on to next PR with comments: {remaining_prs}")
                return

        logger.info("No changes needed")

    def run_loop(self) -> None:
        """Run the main monitoring loop indefinitely until killed.

        Priority order:
        1. First pass: Scan ALL PRs for CI failures (bottom to top)
        2. If any CI failures, process the FIRST (bottom-most) PR with CI failure
           - This ensures the whole stack can build before fixing comments
        3. Only after all CI passes, process Copilot comments
        """
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

                pr_numbers = self.get_stack_prs()  # Bottom-to-top order
                if not pr_numbers:
                    logger.warning("No PRs found in stack. Waiting...")
                    time.sleep(self.config.poll_interval)
                    continue

                logger.info(f"Found PRs (bottom to top): {pr_numbers}")

                # PHASE 1: Scan ALL PRs for CI status first
                logger.info("Phase 1: Scanning for CI failures...")
                pr_statuses: dict[int, PRStatus] = {}
                pr_comments: dict[int, list] = {}

                for pr_number in pr_numbers:
                    pr_statuses[pr_number] = self.get_pr_ci_status(pr_number)
                    pr_comments[pr_number] = self.get_copilot_comments(pr_number)

                # Find PRs with CI failures (bottom to top)
                prs_with_ci_failure = [
                    pr for pr in pr_numbers
                    if pr_statuses[pr].ci_failed
                ]

                # Find PRs with pending CI
                prs_with_ci_pending = [
                    pr for pr in pr_numbers
                    if pr_statuses[pr].ci_pending
                ]

                # Find PRs with unresolved comments (only if CI passes)
                prs_with_comments = [
                    pr for pr in pr_numbers
                    if pr_comments[pr] and not pr_statuses[pr].ci_failed
                ]

                # Log summary
                if prs_with_ci_failure:
                    logger.info(colored(f"CI failures: {prs_with_ci_failure}", Colors.RED))
                if prs_with_ci_pending:
                    logger.info(f"CI pending: {prs_with_ci_pending}")
                if prs_with_comments:
                    logger.info(f"PRs with comments: {prs_with_comments}")

                made_changes = False
                comments_addressed = False

                # PHASE 2: Process CI failures first (bottom to top)
                if prs_with_ci_failure:
                    # Process the FIRST (bottom-most) PR with CI failure
                    # This ensures lower PRs build before we try to fix higher ones
                    target_pr = prs_with_ci_failure[0]
                    target_branch = self.get_branch_for_pr(target_pr)
                    logger.info(colored(f"Phase 2: Fixing CI failure on PR #{target_pr} ({target_branch}) (lowest failing PR)", Colors.YELLOW))

                    # Process with ci_only=True to focus on CI, but also include comments
                    # since we're already making changes to this PR
                    pr_made_changes, pr_comments_addressed = self.process_pr(target_pr, ci_only=False)

                    if pr_made_changes:
                        made_changes = True
                        logger.info(f"Fixed issues in PR #{target_pr}, pushing all branches...")
                        if self.push_stack(current_branch=target_branch):
                            # Reset iteration count since we pushed new commits
                            self.iteration_counts[f"pr_{target_pr}"] = 0
                            self.update_pr_stack_metadata(pr_numbers)  # Ensure stack metadata
                            for pr in pr_numbers:
                                self.request_copilot_review(pr)
                            # Wait for CI to start fresh on new commits
                            logger.info(colored("Stack pushed! Waiting 60s for CI to restart...", Colors.GREEN))
                            time.sleep(60)
                        logger.info(colored("Stack updated! Checking CI status...", Colors.GREEN))

                # PHASE 3: Only if no CI failures, process comments
                elif prs_with_comments:
                    # Process the first PR with unresolved comments
                    target_pr = prs_with_comments[0]
                    target_branch = self.get_branch_for_pr(target_pr)
                    logger.info(colored(f"Phase 3: All CI passing, fixing comments on PR #{target_pr} ({target_branch})", Colors.GREEN))

                    pr_made_changes, pr_comments_addressed = self.process_pr(target_pr)

                    if pr_made_changes:
                        made_changes = True
                        logger.info(f"Fixed comments in PR #{target_pr}, pushing all branches...")
                        if self.push_stack(current_branch=target_branch):
                            # Reset iteration count since we pushed new commits
                            self.iteration_counts[f"pr_{target_pr}"] = 0
                            self.update_pr_stack_metadata(pr_numbers)  # Ensure stack metadata
                            for pr in pr_numbers:
                                self.request_copilot_review(pr)
                            # Wait for CI to start fresh on new commits
                            logger.info(colored("Stack pushed! Waiting 60s for CI to restart...", Colors.GREEN))
                            time.sleep(60)
                        logger.info(colored("Stack updated! Checking CI status...", Colors.GREEN))
                    elif pr_comments_addressed:
                        # All comments were dismissed (no code changes needed)
                        # The PR is now "clean" - log this and continue to next cycle
                        comments_addressed = True
                        logger.info(colored(f"PR #{target_pr}: All comments dismissed, no push needed", Colors.GREEN))

                # Status summary
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


def parse_args() -> Config:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Automatically monitor and fix CI failures and review comments for PR stacks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --status                    Show current stack status
  %(prog)s --once --dry-run            Run once in dry-run mode
  %(prog)s --poll-interval 120         Check every 2 minutes
  %(prog)s --ai-cmd aider              Use aider instead of augie
        """
    )

    parser.add_argument("--poll-interval", type=int, default=60,
                        help="Polling interval in seconds (default: 60)")
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
    )


def main():
    """Main entry point."""
    config = parse_args()

    # Change to repo directory
    os.chdir(config.repo_dir)

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
