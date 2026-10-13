#!/usr/bin/env python3
"""Update the GitHub metrics table in README.md using public repository data.

Counts commits authored by GITHUB_USERNAME on the default branches of public,
non-fork repositories owned by that account. Added-line totals are the sum of
GitHub's per-commit stats.additions values. A commit with no available stats
is skipped for line totals but still counts as a commit.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import os
import re
import sys
import time

import requests

USERNAME = os.environ.get("GITHUB_USERNAME", "Siddharthrai87")
TOKEN = os.environ.get("GITHUB_TOKEN")
README_PATH = "README.md"
API = "https://api.github.com"
YEAR = datetime.now(timezone.utc).year
START_MARKER = "<!-- GITHUB_METRICS_START -->"
END_MARKER = "<!-- GITHUB_METRICS_END -->"

session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "github-profile-metrics-workflow",
})
if TOKEN:
    session.headers["Authorization"] = f"Bearer {TOKEN}"


def get_json(url, params=None, attempts=5):
    """GET JSON with basic handling for transient errors and rate limits."""
    for attempt in range(attempts):
        response = session.get(url, params=params, timeout=30)
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(response.headers.get("X-RateLimit-Reset", "0"))
            wait = max(1, min(reset - int(time.time()) + 1, 60))
            print(f"Rate limit reached; waiting {wait}s.", file=sys.stderr)
            time.sleep(wait)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(2 ** attempt, 30))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"GitHub API request failed after retries: {url}")


def fetch_owned_public_repos():
    repos = []
    page = 1
    while True:
        batch = get_json(
            f"{API}/users/{USERNAME}/repos",
            params={"type": "owner", "per_page": 100, "page": page},
        )
        if not batch:
            break
        repos.extend(
            repo for repo in batch
            if not repo.get("fork", False) and not repo.get("archived", False)
        )
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_commits(repo):
    """Return commits attributed by GitHub to this username on the default branch."""
    commits = []
    page = 1
    while True:
        batch = get_json(
            f"{API}/repos/{USERNAME}/{repo['name']}/commits",
            params={"author": USERNAME, "per_page": 100, "page": page},
        )
        if not batch:
            break
        for item in batch:
            author = item.get("author") or {}
            if author.get("login", "").lower() != USERNAME.lower():
                continue
            commits.append({
                "repo": repo["name"],
                "sha": item["sha"],
                "date": (item.get("commit", {}).get("author") or {}).get("date", ""),
            })
        if len(batch) < 100:
            break
        page += 1
    return commits


def fetch_additions(commit):
    url = f"{API}/repos/{USERNAME}/{commit['repo']}/commits/{commit['sha']}"
    try:
        details = get_json(url)
        return int((details.get("stats") or {}).get("additions", 0))
    except Exception as exc:
        print(f"Could not read stats for {commit['repo']}@{commit['sha'][:7]}: {exc}", file=sys.stderr)
        return None


def main():
    repos = fetch_owned_public_repos()
    print(f"Found {len(repos)} non-fork, non-archived public repositories.")

    commits = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_commits, repo) for repo in repos]
        for future in as_completed(futures):
            commits.extend(future.result())

    all_commits = len(commits)
    this_year_commits = [
        item for item in commits
        if item["date"] and item["date"][:4] == str(YEAR)
    ]

    # Per-commit stats are fetched in parallel. A commit that has unavailable
    # stats is omitted from line totals rather than falsely reported as zero.
    additions_by_commit = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {
            pool.submit(fetch_additions, item): item
            for item in commits
        }
        for future in as_completed(future_map):
            item = future_map[future]
            additions_by_commit[(item["repo"], item["sha"])] = future.result()

    all_line_values = [
        value for value in additions_by_commit.values() if value is not None
    ]
    year_line_values = [
        additions_by_commit[(item["repo"], item["sha"])]
        for item in this_year_commits
        if additions_by_commit.get((item["repo"], item["sha"])) is not None
    ]

    all_lines = sum(all_line_values)
    year_lines = sum(year_line_values)

    table = "\n".join([
        START_MARKER,
        "| Metric | All time | This year |",
        "|:---|---:|---:|",
        f"| **Commits** | {all_commits:,} | {len(this_year_commits):,} ({YEAR}) |",
        f"| **Lines added** | {all_lines:,} | {year_lines:,} ({YEAR}) |",
        END_MARKER,
    ])

    path = README_PATH
    with open(path, "r", encoding="utf-8") as file:
        content = file.read()

    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    if not pattern.search(content):
        raise RuntimeError("Could not find GitHub metrics markers in README.md")

    updated = pattern.sub(lambda _: table, content, count=1)
    with open(path, "w", encoding="utf-8") as file:
        file.write(updated)

    print(f"Updated README metrics for {YEAR}: {all_commits} commits, {all_lines} added lines.")


if __name__ == "__main__":
    main()
