
#!/usr/bin/env python3
"""Update public GitHub metrics in the profile README."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import os
import re
import sys
import time

import requests

USERNAME = os.environ.get("GITHUB_USERNAME", "Siddharthrai87")
TOKEN = os.environ.get("METRICS_TOKEN") or os.environ.get("GITHUB_TOKEN")
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
    """Fetch JSON and report meaningful API errors."""
    for attempt in range(attempts):
        response = session.get(url, params=params, timeout=30)

        if response.status_code == 403:
            message = ""
            try:
                message = response.json().get("message", "")
            except Exception:
                message = response.text

            remaining = response.headers.get(
                "X-RateLimit-Remaining", "unknown"
            )
            reset = response.headers.get(
                "X-RateLimit-Reset", "unknown"
            )

            if remaining == "0":
                wait = max(
                    1, min(int(reset) - int(time.time()) + 1, 60)
                )
                if attempt < attempts - 1:
                    print(
                        f"Rate limit reached; waiting {wait}s.",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue

            raise RuntimeError(
                f"GitHub API 403 at {response.url}: {message}. "
                f"Rate-limit remaining={remaining}; reset={reset}. "
                "Check METRICS_TOKEN and its repository permissions."
            )

        if response.status_code == 429 or response.status_code in (
            500, 502, 503, 504
        ):
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 30))
                continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError(f"GitHub API request failed: {url}")


def fetch_owned_public_repos():
    """List public repositories owned by the user; exclude forks/archives."""
    repos = []
    page = 1

    while True:
        batch = get_json(
            f"{API}/users/{USERNAME}/repos",
            params={
                "type": "owner",
                "per_page": 100,
                "page": page,
            },
        )

        if not batch:
            break

        repos.extend(
            repo for repo in batch
            if not repo.get("fork", False)
            and not repo.get("archived", False)
            and not repo.get("private", False)
        )

        if len(batch) < 100:
            break

        page += 1

    return repos


def fetch_commits(repo):
    """Fetch commits GitHub attributes to this user on the default branch."""
    commits = []
    page = 1

    while True:
        try:
            batch = get_json(
                f"{API}/repos/{USERNAME}/{repo['name']}/commits",
                params={
                    "author": USERNAME,
                    "per_page": 100,
                    "page": page,
                },
            )
        except Exception as exc:
            print(
                f"Skipping commit history for {repo['name']}: {exc}",
                file=sys.stderr,
            )
            return None

        if not batch:
            break

        for item in batch:
            author = item.get("author") or {}

            if author.get("login", "").lower() != USERNAME.lower():
                continue

            commit_date = (
                item.get("commit", {}).get("author") or {}
            ).get("date", "")

            commits.append({
                "repo": repo["name"],
                "sha": item["sha"],
                "date": commit_date,
            })

        if len(batch) < 100:
            break

        page += 1

    return commits


def fetch_additions(commit):
    """Return additions for a commit, or None when stats are unavailable."""
    url = (
        f"{API}/repos/{USERNAME}/{commit['repo']}"
        f"/commits/{commit['sha']}"
    )

    try:
        details = get_json(url)
        stats = details.get("stats") or {}
        if "additions" not in stats:
            return None
        return int(stats["additions"])
    except Exception as exc:
        print(
            f"Skipping line statistics for "
            f"{commit['repo']}@{commit['sha'][:7]}: {exc}",
            file=sys.stderr,
        )
        return None


def main():
    repos = fetch_owned_public_repos()
    print(
        f"Found {len(repos)} non-fork, non-archived public repositories."
    )

    commits = []
    skipped_repos = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_commits, repo): repo
            for repo in repos
        }

        for future in as_completed(futures):
            repo = futures[future]
            result = future.result()

            if result is None:
                skipped_repos.append(repo["name"])
            else:
                commits.extend(result)

    all_commit_count = len(commits)
    this_year_commits = [
        item for item in commits
        if item["date"] and item["date"][:4] == str(YEAR)
    ]

    additions = {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_additions, item): item
            for item in commits
        }

        for future in as_completed(futures):
            item = futures[future]
            additions[(item["repo"], item["sha"])] = future.result()

    all_values = [
        value for value in additions.values()
        if value is not None
    ]

    year_values = [
        additions[(item["repo"], item["sha"])]
        for item in this_year_commits
        if additions.get((item["repo"], item["sha"])) is not None
    ]

    all_lines = sum(all_values)
    year_lines = sum(year_values)

    table = "\n".join([
        START_MARKER,
        "| Metric | All time | This year |",
        "|:---|---:|---:|",
        f"| **Commits** | {all_commit_count:,} | "
        f"{len(this_year_commits):,} ({YEAR}) |",
        f"| **Lines added** | {all_lines:,} | "
        f"{year_lines:,} ({YEAR}) |",
        END_MARKER,
    ])

    with open(README_PATH, "r", encoding="utf-8") as file:
        content = file.read()

    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )

    if not pattern.search(content):
        raise RuntimeError(
            "Metrics markers not found in README.md. "
            "Ensure the README contains GITHUB_METRICS_START and "
            "GITHUB_METRICS_END comments."
        )

    updated = pattern.sub(lambda _: table, content, count=1)

    with open(README_PATH, "w", encoding="utf-8") as file:
        file.write(updated)

    print(f"Updated metrics for {YEAR}.")
    print(f"Commits: {all_commit_count}")
    print(f"Lines added (available stats): {all_lines}")
    print(f"Commits with available line stats: {len(all_values)}")

    if skipped_repos:
        print(
            "Repositories skipped due to API errors: "
            + ", ".join(sorted(skipped_repos))
        )


if __name__ == "__main__":
    main()
