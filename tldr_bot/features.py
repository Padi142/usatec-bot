"""/feature: feature requests from chat, filed as GitHub issues for a later build session."""

import re
from dataclasses import dataclass

import httpx

FEATURE_RE = re.compile(r"^\s*/feature(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)

# Build sessions pick up open issues with this label.
FEATURE_LABEL = "feature-request"
MAX_REQUEST_CHARS = 2000
MAX_TITLE_CHARS = 80


@dataclass(frozen=True)
class FeatureCommand:
    request: str | None = None  # None means "show the usage"


def parse_feature_command(text: str) -> FeatureCommand | None:
    """Parse "/feature <request>". Returns None for other commands."""
    m = FEATURE_RE.match(text)
    return FeatureCommand(request=m[1] or None) if m else None


def issue_title(request: str) -> str:
    """The request's first line, cut at a word boundary to fit an issue title."""
    first = " ".join(request.split("\n", 1)[0].split())
    if len(first) <= MAX_TITLE_CHARS:
        return first
    return first[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def issue_body(request: str, requester: str) -> str:
    # Only the display name: the repo is public, so phone numbers and ids stay out.
    return f"{request}\n\n---\nRequested on Signal by {requester} with /feature."


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class Issue:
    number: int
    url: str


class GitHubIssues:
    """Files issues in one repository with a token that can write its issues."""

    def __init__(self, token: str, repo: str, timeout: float = 30):
        self.repo = repo
        self._http = httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def create(self, title: str, body: str, labels: list[str]) -> Issue:
        try:
            resp = await self._http.post(
                f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels}
            )
            resp.raise_for_status()
            data = resp.json()
            return Issue(number=int(data["number"]), url=str(data["html_url"]))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise GitHubError(f"{type(e).__name__}: {e}") from e
