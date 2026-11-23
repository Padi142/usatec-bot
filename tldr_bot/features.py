"""/feature: feature requests from chat, filed as GitHub issues for a later build session."""

import re
from dataclasses import dataclass

import httpx

FEATURE_RE = re.compile(r"^\s*/feature(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)

# Build sessions pick up open issues with this label.
FEATURE_LABEL = "feature-request"
MAX_REQUEST_CHARS = 2000
MAX_TITLE_CHARS = 80
# A sender's next request has to wait this long, so one person can't flood the public tracker.
COOLDOWN_SECONDS = 300

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


class GitHubIssues:
    """Files issues in one repository with a token that can write its issues."""

    def __init__(self, token: str, repo: str, timeout: float = 30):
        self.repo = repo
        self.requests_url = f"https://github.com/{repo}/issues?q=is%3Aopen+label%3A{FEATURE_LABEL}"
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
