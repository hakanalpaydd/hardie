"""AI agent invocation and context building."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Tuple

from hardie.utils import Colors, colored, logger

if TYPE_CHECKING:
    from hardie.config import Config


class AIAgent:
    """Handles AI agent invocation and prompt building."""

    def __init__(self, config: "Config", run_command_fn, run_gh_fn, fetch_log_fn):
        self.config = config
        self.run_command = run_command_fn
        self.run_gh = run_gh_fn
        self.fetch_buildkite_log = fetch_log_fn

    def build_pr_issues_context(
        self,
        pr_number: int,
        failed_checks: list[dict],
        comments: list[dict]
    ) -> str:
        """Build context string for AI to fix ALL issues in a PR at once."""
        lines = [
            "# ⚠️ SPEED RULES - READ FIRST ⚠️",
            "",
            "- DO NOT use `find` commands (too slow)",
            "- DO NOT use `github-api` tool (IP blocked - use `gh` CLI)",
            "- DO NOT run `rushx build` (takes 15+ min)",
            "- DO NOT run `rushx typecheck` or `tsc` (pre-existing errors)",
            "- DO NOT search for files - paths are provided below",
            "- If running Jest tests, use `--runInBand --forceExit` flags",
            "",
            "---",
            "",
            f"# All Issues in PR #{pr_number}",
            "",
        ]

        # Add CI failures section
        if failed_checks:
            lines.extend(["## 🔴 CI FAILURES (fix these first!):", ""])
            for check in failed_checks:
                lines.append(f"### CI: {check['name']} - {check['state']}")
                url = check.get('url', '')
                if url:
                    lines.append(f"Build URL: {url}")
                    logger.info(f"Fetching error log for {check['name']}...")
                    log_content = self.fetch_buildkite_log(url)
                    if log_content:
                        lines.extend([
                            "", "**Error Output:**", "```",
                            log_content[:4000], "```",
                        ])
                    else:
                        lines.append("(Could not fetch error log)")
                lines.append("")

        # Add review comments section
        if comments:
            lines.extend(["## 💬 REVIEW COMMENTS:", ""])
            for i, comment in enumerate(comments, 1):
                file_path = comment.get("path", "")
                line_num = comment.get("line")
                body = comment.get("body", "")
                thread_id = comment.get("thread_id", "")

                lines.extend([
                    f"### Comment #{i}: `{file_path}` line {line_num}",
                    f"Thread ID: {thread_id}", "",
                    "**Copilot says:**", "```", body[:1000], "```", "",
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
            "---", "",
            "## Instructions:", "",
            "**Fix ALL issues listed above in this single session.**", "",
            "### For CI failures:",
            "- Read the error output and fix the code",
            "- Validate with `cd services/consumer-web-next && rushx eslint` (NOT es-lint)", "",
            "### For each review comment, decide: FIX or DISMISS?", "",
            "**FIX if:** The comment is valid and improves PRODUCTION code quality",
            "**DISMISS if (be aggressive about dismissing low-value feedback):**",
            "- Comments about mock data, test content, or placeholder text (these are not production code)",
            "- Trivial wording/phrasing suggestions (e.g., 'value props' vs 'value propositions')",
            "- Issues about component registration that appear to already be working in code",
            "- Suggestions that would require major architectural changes out of scope",
            "- Overly pedantic stylistic preferences that don't affect functionality",
            "- Comments about code in a DIFFERENT file than the one being reviewed in this PR",
            "- Suggestions based on incorrect assumptions about the codebase", "",
            "⚡ SPEED TIP: If you're unsure, DISMISS. It's better to dismiss 5 pedantic comments quickly than spend 10 minutes on each. The reviewer can re-open if it's truly important.", "",
            "### Required Output Format:", "",
            "After addressing ALL issues, output a summary block like this:", "",
            "```", "ISSUES_SUMMARY:",
            "- CI: FIXED (brief description)",
            "- Comment #1 (thread_id): FIXED",
            "- Comment #2 (thread_id): DISMISSED:reason",
            "- Comment #3 (thread_id): FIXED", "```", "",
            "**IMPORTANT:**",
            "- Include the thread_id in parentheses for each comment",
            "- Use FIXED or DISMISSED:reason for each",
            "- Do NOT commit - just make the code changes",
        ])

        return "\n".join(lines)

    def build_comment_fix_context(self, pr_number: int, comment: dict) -> str:
        """Build context string for AI to address a single review comment."""
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
            "- DO NOT run `rushx typecheck` or `tsc` (pre-existing errors)",
            "- DO NOT search for files - the path and code are provided below",
            "- If running Jest tests, use `--runInBand --forceExit` flags",
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
            "```", body, "```", "",
        ]

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
            "", "## Instructions:", "",
            "**IMPORTANT: The comment and code are shown above. Do NOT fetch additional data.**", "",
            "You must decide: Is this comment worth fixing, or should it be dismissed?", "",
            "### If the comment is VALID and should be FIXED:",
            "1. Make the necessary code changes to address the feedback",
            "2. For validation, use ONLY fast checks:",
            "   - `rushx eslint` for lint errors (NOT es-lint)",
            "   - Do NOT run tsc or typecheck (slow and has pre-existing errors)",
            "3. After fixing, output exactly: COMMENT_ACTION: FIXED", "",
            "### If the comment should be DISMISSED (not worth fixing):",
            "**Be aggressive about dismissing low-value feedback:**",
            "- Comments about mock data, test content, or placeholder text (NOT production code)",
            "- Trivial wording/phrasing suggestions that don't affect functionality",
            "- Issues about component registration that appear to already be working",
            "- Suggestions requiring major architectural refactoring out of scope",
            "- Overly pedantic stylistic preferences",
            "- Comments about code in a different file or PR",
            "- Suggestions based on incorrect assumptions about the codebase", "",
            "⚡ If you're unsure, DISMISS. The reviewer can re-open if truly important.", "",
            "If dismissing, output exactly:",
            "COMMENT_ACTION: DISMISSED:<brief reason>", "",
            "Examples:",
            "- COMMENT_ACTION: DISMISSED:Mock data content is not production code",
            "- COMMENT_ACTION: DISMISSED:Trivial wording preference",
            "- COMMENT_ACTION: DISMISSED:Component registration is already working", "",
            "**DO NOT commit changes - just make the code changes if fixing.**",
        ])

        return "\n".join(lines)

    def invoke_ai_agent(self, context: str, issue_type: str) -> Tuple[bool, dict]:
        """Invoke the AI agent to fix issues.

        Returns:
            Tuple of (success, issues_results):
            - success: True if AI ran successfully
            - issues_results: Dict mapping thread_id/issue_key to {"action": "FIXED"|"DISMISSED", "reason": str}
        """
        logger.info(f"Invoking AI agent to fix {issue_type}...")

        if self.config.dry_run:
            logger.info(colored("[DRY RUN] Would invoke AI with context:", Colors.YELLOW))
            print(context[:500] + "..." if len(context) > 500 else context)
            return (True, {"_dry_run": {"action": "FIXED", "reason": ""}})

        result = self.run_command(["which", self.config.ai_cmd])
        if result.returncode != 0:
            logger.error(f"AI command '{self.config.ai_cmd}' not found")
            return (False, {})

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(context)
            prompt_file = f.name

        output_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False).name

        try:
            ai_cmd = self.config.ai_cmd

            if "auggie" in ai_cmd:
                cmd = [ai_cmd, "--print", "--instruction-file", prompt_file]
            elif "claude" in ai_cmd:
                cmd = [ai_cmd, "--print", context[:8000]]
            elif "aider" in ai_cmd:
                cmd = [ai_cmd, "--message-file", prompt_file]
            else:
                cmd = [ai_cmd, context[:4000]]

            logger.info(f"Running: {cmd[0]} {cmd[1] if len(cmd) > 1 else ''} ...")

            with open(output_file, 'w') as out_f:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    cwd=str(self.config.repo_dir), text=True
                )

                output_lines = []
                for line in proc.stdout:
                    print(line, end='')
                    output_lines.append(line)
                    out_f.write(line)

                proc.wait()
                result_code = proc.returncode
                output = ''.join(output_lines)

            if result_code != 0:
                logger.error(f"AI command failed with exit code {result_code}")
                return (False, {})

            return (True, self._parse_ai_output(output))

        except Exception as e:
            logger.error(f"Error invoking AI agent: {e}")
            return (False, {})
        finally:
            os.unlink(prompt_file)
            try:
                os.unlink(output_file)
            except Exception:
                pass

    def _parse_ai_output(self, output: str) -> dict:
        """Parse AI output for ISSUES_SUMMARY or COMMENT_ACTION."""
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
                            break
                        continue
                    if line.startswith("- "):
                        line = line[2:]
                    if ":" in line:
                        key_part, action_part = line.rsplit(":", 1)
                        action_part = action_part.strip()
                        if "(" in key_part and ")" in key_part:
                            thread_id = key_part.split("(")[1].split(")")[0].strip()
                        else:
                            thread_id = key_part.strip()

                        if action_part.startswith("FIXED"):
                            issues_results[thread_id] = {"action": "FIXED", "reason": ""}
                        elif action_part.startswith("DISMISSED"):
                            reason = action_part.split(":", 1)[1].strip() if ":" in action_part else ""
                            issues_results[thread_id] = {"action": "DISMISSED", "reason": reason}

        # Fallback: check for old COMMENT_ACTION format
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

        return issues_results

