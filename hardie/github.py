"""GitHub and Copilot API interactions."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional, Tuple

from hardie.config import PRStatus
from hardie.utils import Colors, colored, logger

if TYPE_CHECKING:
    from hardie.config import Config


class GitHubClient:
    """Handles all GitHub API interactions."""

    def __init__(self, config: "Config", run_command_fn, run_gh_fn):
        self.config = config
        self.run_command = run_command_fn
        self.run_gh = run_gh_fn
        self._owner_repo_cache: Optional[Tuple[str, str]] = None

    def get_owner_repo(self) -> Tuple[str, str]:
        """Get the owner and repo from git remote URL (cached)."""
        if self._owner_repo_cache:
            return self._owner_repo_cache

        remote_result = self.run_command(["git", "remote", "get-url", "origin"])
        if remote_result.returncode != 0:
            return ("", "")

        remote_url = remote_result.stdout.strip()
        # Parse git@github.com:owner/repo.git or https://github.com/owner/repo.git
        match = re.search(r'[:/]([^/]+)/([^/.]+)(?:\.git)?$', remote_url)
        if not match:
            return ("", "")

        self._owner_repo_cache = match.groups()
        return self._owner_repo_cache

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
        """Get unresolved review threads from Copilot on a PR using GraphQL."""
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
            if thread.get("isResolved", False):
                continue

            thread_id = thread.get("id", "")
            comments = thread.get("comments", {}).get("nodes", [])
            if not comments:
                continue

            first_comment = comments[0]
            author = first_comment.get("author", {}).get("login", "")
            if author != "copilot-pull-request-reviewer":
                continue

            copilot_comments.append({
                "thread_id": thread_id,
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

    def request_copilot_review(self, pr_number: int) -> None:
        """Request a new review from Copilot."""
        if self.config.dry_run:
            logger.info(colored(f"[DRY RUN] Would request Copilot review for #{pr_number}", Colors.YELLOW))
            return

        logger.info(f"Requesting Copilot review for PR #{pr_number}...")
        self.run_gh([
            "pr", "edit", str(pr_number),
            "--add-reviewer", "copilot-pull-request-reviewer"
        ])

    def get_branch_for_pr(self, pr_number: int) -> Optional[str]:
        """Get the branch name for a PR number."""
        data = self.run_gh(["pr", "view", str(pr_number), "--json", "headRefName"])
        if data and isinstance(data, dict):
            return data.get("headRefName")
        return None

    def _get_branch_head_sha(self, branch_name: str) -> str:
        """Get the current HEAD commit SHA for a branch."""
        result = self.run_command(
            ["git", "rev-parse", f"origin/{branch_name}"],
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        result = self.run_command(
            ["git", "rev-parse", branch_name],
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def generate_stack_table(self, pr_numbers: list[int], current_pr: int,
                             parent_pr: Optional[int] = None) -> str:
        """Generate the av-style stack visualization table for a PR description."""
        pr_list_reversed = list(reversed(pr_numbers))

        lines = []
        for pr in pr_list_reversed:
            if pr == current_pr:
                lines.append(f"* ➡️ **#{pr}**")
            else:
                lines.append(f"* **#{pr}**")
        lines.append("* `master`")

        pr_list_md = "\n".join(lines)
        depends_text = f"<b>Depends on #{parent_pr}.</b> " if parent_pr else ""

        return f"""<!-- av pr stack begin -->
<table><tr><td><details><summary>{depends_text}This PR is part of a stack created with <a href="https://github.com/aviator-co/av">Aviator</a>.</summary>

{pr_list_md}
</details></td></tr></table>
<!-- av pr stack end -->"""

    def generate_metadata_json(self, pr_number: int, pr_numbers: list[int],
                               branch_name: str) -> str:
        """Generate the av pr metadata JSON block for a PR description."""
        idx = pr_numbers.index(pr_number) if pr_number in pr_numbers else -1

        if idx == 0:
            parent = "master"
            parent_head = self._get_branch_head_sha("master")
            parent_pull = None
        elif idx > 0:
            parent_pr = pr_numbers[idx - 1]
            data = self.run_gh(["pr", "view", str(parent_pr), "--json", "headRefName"])
            parent = data.get("headRefName", "master") if data else "master"
            parent_head = self._get_branch_head_sha(parent)
            parent_pull = parent_pr
        else:
            parent = "master"
            parent_head = self._get_branch_head_sha("master")
            parent_pull = None

        metadata = {"parent": parent, "parentHead": parent_head, "trunk": "master"}
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
        """Update all PRs in the stack to have proper stack metadata."""
        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would update PR stack metadata", Colors.YELLOW))
            return

        if not pr_numbers:
            return

        logger.info(f"Updating stack metadata for {len(pr_numbers)} PRs...")

        for i, pr_number in enumerate(pr_numbers):
            data = self.run_gh(["pr", "view", str(pr_number), "--json", "body,headRefName"])
            if not data:
                logger.warning(f"Failed to get PR #{pr_number} body")
                continue

            current_body = data.get("body", "") or ""
            branch_name = data.get("headRefName", "")
            parent_pr = pr_numbers[i - 1] if i > 0 else None

            new_stack_table = self.generate_stack_table(pr_numbers, pr_number, parent_pr)
            new_metadata = self.generate_metadata_json(pr_number, pr_numbers, branch_name)

            has_stack_table = "<!-- av pr stack begin -->" in current_body
            has_metadata = "<!-- av pr metadata" in current_body

            new_body = current_body

            if has_stack_table:
                new_body = re.sub(
                    r'<!-- av pr stack begin -->.*?<!-- av pr stack end -->',
                    new_stack_table, new_body, flags=re.DOTALL
                )
            else:
                new_body = new_stack_table + "\n\n" + new_body

            if has_metadata:
                new_body = re.sub(
                    r'<!-- av pr metadata.*?-->',
                    new_metadata, new_body, flags=re.DOTALL
                )
            else:
                new_body = new_body.rstrip() + "\n\n" + new_metadata

            if new_body != current_body:
                logger.info(f"Updating metadata for PR #{pr_number}...")
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

