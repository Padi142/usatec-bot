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
CAT_GEN_MODELS = (CAT_GEN_MODEL, "cat-diffusion", CAT_GEN_PROMPT_MODEL)
GRID_LAYOUTS = ("2x2", "3x3", "4x4", "5x5")
PROMPT_MAX_LENGTH = 500
SEED_MAX = 2**32 - 1


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
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise CatError(f"{type(e).__name__}: {e}") from e
        if not isinstance(data, dict):
            raise CatError(f"Unexpected response from {path}: {data!r:.200}")
        return data

    async def generate(
        self, prompt: str | None = None, layout: str | None = None,
        *, model: str | None = None, seed: int | None = None,
    ) -> tuple[str, bytes]:
        """Generate a cat picture and wait for the job to finish. Returns (content type, image bytes).

        Layouts from 2x2 through 5x5 make one grid image; only models without a prompt support them.
        The default model follows the prompt. Omitted seeds are randomized by the API.
        """
        body = self._job_body(prompt, layout, model, seed)
        try:
            async with asyncio.timeout(self.timeout):
                url = await self._run_job(body)
                resp = await self._http.get(url)  # No auth header: the image lives on another host.
                resp.raise_for_status()
        except TimeoutError as e:
            raise CatError(f"Timed out after {self.timeout}s") from e
        except httpx.HTTPError as e:
            raise CatError(f"{type(e).__name__}: {e}") from e
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            raise CatError(f"Expected an image, got {content_type or 'no content type'}")
        return content_type, resp.content

    @staticmethod
    def _job_body(
        prompt: str | None, layout: str | None, model: str | None, seed: int | None,
    ) -> dict[str, str | int]:
        prompt = prompt.strip() if prompt else None
        model = model or (CAT_GEN_PROMPT_MODEL if prompt else CAT_GEN_MODEL)
        if model not in CAT_GEN_MODELS:
            raise ValueError("Unknown cat generation model.")
        if layout is not None and layout not in ("single", *GRID_LAYOUTS):
            raise ValueError("Choose a single cat or a grid from 2x2 through 5x5.")
        if model == CAT_GEN_PROMPT_MODEL:
            if not prompt:
                raise ValueError("The text-to-cat model needs a prompt.")
            if layout in GRID_LAYOUTS:
                raise ValueError("Grids only work without a prompt, using a cat-diffusion model.")
        elif prompt:
            raise ValueError("Cat-diffusion models do not take a prompt. Use a text-to-cat model for prompts.")
        if seed is not None and (type(seed) is not int or not 0 <= seed <= SEED_MAX):
            raise ValueError(f"Seed must be an integer between 0 and {SEED_MAX}.")
        body: dict[str, str | int] = {"model": model}
        if prompt:
            body["prompt"] = prompt[:PROMPT_MAX_LENGTH]
        if layout:
            body["layout"] = layout
        if seed is not None:
            body["seed"] = seed
        return body

    async def _run_job(self, body: dict[str, str | int]) -> str:
        """Create a job and poll it until it's done. Returns the image URL."""
        job = await self._api("POST", "/api/v1/jobs", json=body)
        job_id = job.get("id")
        if not job_id:
            raise CatError(f"Job response has no id: {job!r:.200}")

        while job.get("state") != "done":
            if job.get("error") or job.get("state") in ("failed", "error", "cancelled", "canceled", "expired"):
                raise CatError(f"Job {job_id} {job.get('state')}: {job.get('error')}")
            await asyncio.sleep(self.poll_interval)
            job = await self._api("GET", f"/api/v1/jobs/{job_id}")

        try:
            return job["images"][0]["url"]
        except (KeyError, IndexError, TypeError) as e:
            raise CatError(f"Job {job_id} is done but has no image") from e

