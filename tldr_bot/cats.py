"""Random cat pictures from https://kocicividea.cz/."""

import httpx

CAT_URL = "https://kocicividea.cz/macka"


class CatError(Exception):
    pass


class CatClient:
    def __init__(self, url: str = CAT_URL, timeout: float = 30):
        self.url = url
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def random_cat(self) -> tuple[str, bytes]:
        """Download a random cat picture. Returns (content type, image bytes)."""
        try:
            resp = await self._http.get(self.url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise CatError(f"{type(e).__name__}: {e}") from e
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            raise CatError(f"Expected an image, got {content_type or 'no content type'}")
        return content_type, resp.content
