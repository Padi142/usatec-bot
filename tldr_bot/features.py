"""/feature and deploy announcements: GitHub issues and recent commits."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

FEATURE_RE = re.compile(r"^\s*/feature(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)

# Build sessions pick up open issues with this label.
FEATURE_LABEL = "feature-request"
MAX_REQUEST_CHARS = 2000
MAX_TITLE_CHARS = 80
# A sender's next request has to wait this long, so one person can't flood the public tracker.
COOLDOWN_SECONDS = 300

# Deploy announcements summarize at most this many commits since the previous deploy.
MAX_ANNOUNCED_COMMITS = 20
MAX_COMMIT_CHARS = 8_000
MAX_SINGLE_COMMIT_CHARS = 1_000

# Anything that could be a phone number. Signal falls back to numbers for names and mentions.
PHONE_RE = re.compile(r"\+?\d[\d ()./-]{6,}\d")


@dataclass(frozen=True)
class FeatureCommand:
    request: str | None = None  # None means "show the usage"


def parse_feature_command(text: str) -> FeatureCommand | None:
    """Parse "/feature <request>". Returns None for other commands."""
    m = FEATURE_RE.match(text)
    return FeatureCommand(request=m[1] or None) if m else None


def public_text(text: str) -> str:
    """Make chat text safe to post in the public repo.

    Phone numbers are masked, and a zero-width space after each @ keeps GitHub from
    notifying anyone the text mentions.
    """
    return PHONE_RE.sub("[number]", text).replace("@", "@\u200b")


def issue_title(request: str) -> str:
    """The request's first line, cut at a word boundary to fit an issue title."""
    first = " ".join(public_text(request).split("\n", 1)[0].split())
    if len(first) <= MAX_TITLE_CHARS:
        return first
    return first[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def issue_body(request: str, requester: str) -> str:
    """The request and who sent it. Only a display name, never a Signal id."""
    return f"{public_text(request)}\n\n---\nRequested on Signal by {public_text(requester)} with /feature."


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class Issue:
    number: int
    url: str
    labels: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str

    @property
    def title(self) -> str:
        return self.message.split("\n", 1)[0].strip()


def format_commit_messages(commits: Sequence[Commit], max_chars: int = MAX_COMMIT_CHARS) -> str:
    """Join commit titles and bodies for the LLM, trimming oversized messages and total length."""
    parts: list[str] = []
    total = 0
    for commit in commits:
        entry = commit.message.strip()[:MAX_SINGLE_COMMIT_CHARS]
        if not entry:
            continue
        if parts and total + len(entry) + 2 > max_chars:
            break
        parts.append(entry[:max_chars] if not parts else entry)
        total += len(parts[-1]) + 2
    return "\n\n".join(parts)


class GitHubIssues:
    """GitHub client for one repository: reads commits and, with a token, files issues."""

    def __init__(self, token: str | None, repo: str, timeout: float = 30):
        self.token = token
        self.repo = repo
        self.requests_url = f"https://github.com/{repo}/issues?q=is%3Aopen+label%3A{FEATURE_LABEL}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=timeout,
            headers=headers,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def create(self, title: str, body: str, labels: list[str]) -> Issue:
        """Create an issue. GitHub silently drops labels the token isn't allowed to set."""
        try:
            resp = await self._http.post(
                f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels}
            )
            resp.raise_for_status()
            data = resp.json()
            return Issue(
                number=int(data["number"]),
                url=str(data["html_url"]),
                labels=frozenset(str(label["name"]) for label in data.get("labels", [])),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise GitHubError(f"{type(e).__name__}: {e}") from e

    async def recent_commits(self, sha: str = "master", per_page: int = MAX_ANNOUNCED_COMMITS + 1) -> list[Commit]:
        """The newest commits on `sha`, newest first. Works without a token on public repos."""
        try:
            resp = await self._http.get(
                f"/repos/{self.repo}/commits", params={"sha": sha, "per_page": per_page}
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise TypeError("commits response is not a list")
            return [
                Commit(sha=str(item["sha"]), message=str(item["commit"]["message"]).strip())
                for item in data
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise GitHubError(f"{type(e).__name__}: {e}") from e


GitHubClient = GitHubIssues
