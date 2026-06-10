"""
Fetch merged bot PRs from israelvince/thin-slice and print a summary table.
Requires GITHUB_TOKEN in the environment.
"""
import json
import os
import re
import urllib.request

REPO = "israelvince/thin-slice"
BOT_PREFIX = re.compile(r"(\[Thin[- ]Slice\]|Thin-Slice:)", re.IGNORECASE)

token = os.environ.get("GITHUB_TOKEN", "")
if not token:
    raise SystemExit("ERROR: GITHUB_TOKEN environment variable is not set.")

HEADERS = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def gh_get(url: str) -> list | dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_merged_prs() -> list:
    prs, page = [], 1
    while True:
        url = (
            f"https://api.github.com/repos/{REPO}/pulls"
            f"?state=closed&per_page=100&page={page}"
        )
        batch = gh_get(url)
        if not batch:
            break
        prs.extend(p for p in batch if p.get("merged_at"))
        if len(batch) < 100:
            break
        page += 1
    return prs


def extract_files(body: str) -> str:
    hits = re.findall(r"`([^`]+\.[a-zA-Z0-9_]+)`", body)
    seen, result = set(), []
    for f in hits:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return ", ".join(result) if result else "—"


def extract_tokens(body: str) -> str:
    m = re.search(r"[Tt]okens?\D{0,10}([\d,]+)", body)
    return m.group(1).replace(",", "") if m else "—"


def extract_cost(body: str) -> str:
    m = re.search(r"\$\s*([\d.]+)", body)
    return f"${m.group(1)}" if m else "—"


def main() -> None:
    print(f"Fetching merged PRs from {REPO} …")
    all_prs = fetch_merged_prs()
    print(f"  Total merged : {len(all_prs)}")

    bot_prs = [p for p in all_prs if BOT_PREFIX.match(p["title"])]
    print(f"  Bot PRs      : {len(bot_prs)}\n")

    if not bot_prs:
        print("No bot PRs found.")
        return

    # ── table ──────────────────────────────────────────────────────────────────
    C = {"pr": 6, "title": 52, "files": 42, "tokens": 9, "cost": 8}

    def row(pr, title, files, tokens, cost):
        return (
            f"{pr:<{C['pr']}} "
            f"{title:<{C['title']}} "
            f"{files:<{C['files']}} "
            f"{tokens:<{C['tokens']}} "
            f"{cost:<{C['cost']}}"
        )

    header = row("PR", "Title", "Files", "Tokens", "Cost")
    print(header)
    print("-" * len(header))

    for p in bot_prs:
        body = p.get("body") or ""
        print(row(
            f"#{p['number']}",
            p["title"][:C["title"]],
            extract_files(body)[:C["files"]],
            extract_tokens(body),
            extract_cost(body),
        ))


if __name__ == "__main__":
    main()
