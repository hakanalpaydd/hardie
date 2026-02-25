"""Buildkite log fetching functionality."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple
from urllib.parse import urlparse

from hardie.utils import logger

if TYPE_CHECKING:
    from hardie.config import Config
    import subprocess


class BuildkiteFetcher:
    """Handles fetching logs from Buildkite CI."""

    def __init__(self, config: "Config", run_command_fn):
        self.config = config
        self.run_command = run_command_fn

    def parse_url(self, url: str) -> Optional[Tuple[str, str, str, str]]:
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

    def fetch_log_bklog(self, org: str, pipeline: str, build: str, job_id: str) -> Optional[str]:
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
                "-org", org, "-pipeline", pipeline,
                "-build", build, "-job", job_id,
                "-parquet", str(parquet_file)
            ]

            result = self.run_command(cmd, timeout=120)
            if result.returncode != 0:
                logger.debug(f"bklog parse failed: {result.stderr}")
                return None

            # Query for specific errors with context
            patterns = [
                "Type error:", "Failed to compile",
                "error\\[E", "FAILURE:",
            ]

            for pattern in patterns:
                cmd = [
                    self.config.bklog_cmd, "query",
                    "-file", str(parquet_file),
                    "-op", "search", "-pattern", pattern, "-C", "15"
                ]
                result = self.run_command(cmd, timeout=30)
                if result.returncode == 0 and result.stdout.strip() and "Matches found:" in result.stdout:
                    return result.stdout

            # If no specific matches, get the tail
            cmd = [
                self.config.bklog_cmd, "query",
                "-file", str(parquet_file),
                "-op", "tail", "-tail", "100"
            ]
            result = self.run_command(cmd, timeout=30)
            return result.stdout if result.returncode == 0 else None

        except Exception as e:
            logger.debug(f"bklog fetch failed: {e}")
            return None

    def fetch_log_bk(self, org: str, pipeline: str, build: str, job_id: str) -> Optional[str]:
        """Fetch Buildkite log using bk CLI (simpler fallback)."""
        try:
            cmd = [
                self.config.bk_cmd, "job", "log", job_id,
                "-p", f"{org}/{pipeline}", "-b", build, "--no-timestamps"
            ]
            result = self.run_command(cmd, timeout=120)
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                return '\n'.join(lines[-200:])
            logger.debug(f"bk job log failed: {result.stderr}")
            return None
        except Exception as e:
            logger.debug(f"bk fetch failed: {e}")
            return None

    def fetch_log_cookies(self, org: str, pipeline: str, build: str, job_id: str) -> Optional[str]:
        """Fetch Buildkite log using browser cookies (fallback)."""
        try:
            import browser_cookie3
            import requests
        except ImportError:
            logger.debug("browser_cookie3 or requests not installed")
            return None

        try:
            download_url = (
                f"https://buildkite.com/organizations/{org}/pipelines/{pipeline}"
                f"/builds/{build}/jobs/{job_id}/download.txt"
            )
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
            return None
        except Exception as e:
            logger.debug(f"Cookie-based fetch failed: {e}")
            return None

    def fetch_log(self, url: str) -> Optional[str]:
        """Fetch Buildkite log with cascading fallback.

        Tries in order:
        1. bklog (best - caching and smart search)
        2. bk CLI (simpler, still works)
        3. Cookie-based HTTP fetch (when API is unavailable)
        """
        parsed = self.parse_url(url)
        if not parsed:
            logger.debug(f"Could not parse Buildkite URL: {url}")
            return None

        org, pipeline, build, job_id = parsed
        if not job_id:
            logger.debug(f"No job ID in URL: {url}")
            return None

        logger.debug(f"Fetching Buildkite log: {org}/{pipeline} build {build} job {job_id}")

        # Try bklog first (best)
        log = self.fetch_log_bklog(org, pipeline, build, job_id)
        if log:
            logger.debug("Successfully fetched log using bklog")
            return log

        # Try bk CLI
        log = self.fetch_log_bk(org, pipeline, build, job_id)
        if log:
            logger.debug("Successfully fetched log using bk CLI")
            return log

        # Try cookie-based fetch
        log = self.fetch_log_cookies(org, pipeline, build, job_id)
        if log:
            logger.debug("Successfully fetched log using cookies")
            return log

        logger.warning(f"Could not fetch Buildkite log for {url}")
        return None

