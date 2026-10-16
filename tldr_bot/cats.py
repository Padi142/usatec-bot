"""Cat pictures from https://kocicividea.cz/."""

import asyncio

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


CAT_GEN_URL = "https://research.kocicividea.cz"
# Random cats, used when there's no prompt.
CAT_GEN_MODEL = "cat-diffusion-v2"
# Text-to-cat, used with a prompt.
CAT_GEN_PROMPT_MODEL = "flowmatch-cats-tti"
PROMPT_MAX_LENGTH = 500


class CatGenClient:
    """Generated cat pictures from the nano-catana API (research.kocicividea.cz)."""

    def __init__(self, api_key: str, base_url: str = CAT_GEN_URL, timeout: float = 300, poll_interval: float = 2):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._auth = {"Authorization": f"Bearer {api_key}"}
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def _api(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = await self._http.request(method, path, headers=self._auth, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise CatError(f"{type(e).__name__}: {e}") from e

    async def generate(self, prompt: str | None = None) -> tuple[str, bytes]:
        """Generate a cat picture and wait for the job to finish. Returns (content type, image bytes)."""
        if prompt:
            body = {"model": CAT_GEN_PROMPT_MODEL, "prompt": prompt[:PROMPT_MAX_LENGTH]}
        else:
            body = {"model": CAT_GEN_MODEL}
        job = await self._api("POST", "/api/v1/jobs", json=body)

        deadline = asyncio.get_running_loop().time() + self.timeout
        while job.get("state") != "done":
            if job.get("error") or job.get("state") in ("failed", "error", "cancelled", "canceled"):
                raise CatError(f"Job {job.get('id')} {job['state']}: {job.get('error')}")
            if asyncio.get_running_loop().time() > deadline:
                raise CatError(f"Job {job.get('id')} timed out in state {job.get('state')}")
            await asyncio.sleep(self.poll_interval)
            job = await self._api("GET", f"/api/v1/jobs/{job['id']}")

        try:
            url = job["images"][0]["url"]
        except (KeyError, IndexError, TypeError) as e:
            raise CatError(f"Job {job.get('id')} is done but has no image") from e
        try:
            # No auth header here: the image may live on another host.
            resp = await self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise CatError(f"{type(e).__name__}: {e}") from e
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/png"
        return content_type, resp.content

