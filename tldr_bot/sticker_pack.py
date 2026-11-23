"""/sticker: turn images from the chat into stickers of one shared Signal sticker pack.

Signal sticker packs can't change once uploaded. Every edit uploads the whole pack again
as a new pack, with a new link; the bot keeps the images, so it can rebuild the pack.

signal-cli can upload packs, but the REST API doesn't expose that, so the bot uploads
them itself with the account's credentials, read from signal-cli's data directory.
"""

import asyncio
import io
import json
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from .stickers import SIGNAL_CA, SIGNAL_CDN, StickerError, build_manifest, encrypt
from .store import PackSticker

SIGNAL_SERVICE = "https://chat.signal.org"
PACK_TITLE = "Usatec stickers"
PACK_AUTHOR = "usatec-bot"
DEFAULT_EMOJI = "🙂"
# Signal's limits: 200 stickers (plus a cover) per pack, 300 KiB per sticker.
MAX_STICKERS = 200
MAX_STICKER_BYTES = 300 * 1024
STICKER_SIZE = 512
STICKER_CONTENT_TYPE = "image/webp"
# Uploads to the CDN that run at once.
UPLOAD_CONCURRENCY = 8

STICKER_RE = re.compile(r"^\s*/sticker(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class StickerAdd:
    emoji: str | None = None  # None means the default emoji


@dataclass(frozen=True)
class StickerRemove:
    position: int  # 1-based, in the order the pack shows its stickers


@dataclass(frozen=True)
class StickerUsage:
    pass


StickerCommand = StickerAdd | StickerRemove | StickerUsage


def parse_sticker_command(text: str) -> StickerCommand | None:
    """Parse "/sticker [emoji]" or "/sticker remove <n>". Returns None for other commands.

    Whether a bare "/sticker" adds an image or shows the usage depends on whether
    there's an image, so that's for the caller to decide.
    """
    m = STICKER_RE.match(text)
    if not m:
        return None
    args = (m[1] or "").split()
    match args:
        case []:
            return StickerAdd()
        case ["remove", n] if n.isdigit() and int(n) >= 1:
            return StickerRemove(int(n))
        case [emoji] if emoji.lower() != "remove":
            return StickerAdd(emoji)
        case _:
            return StickerUsage()


def make_sticker(image: bytes) -> bytes:
    """Fit an image into a transparent 512x512 WebP under Signal's size limit. Raises StickerError."""
    try:
        with Image.open(io.BytesIO(image)) as img:
            # Phone photos are often stored sideways with an EXIF note to rotate them.
            frame = ImageOps.contain(ImageOps.exif_transpose(img).convert("RGBA"), (STICKER_SIZE, STICKER_SIZE))
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise StickerError(f"Not an image I can read: {e}") from e
    canvas = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0))
    canvas.paste(frame, ((STICKER_SIZE - frame.width) // 2, (STICKER_SIZE - frame.height) // 2))
    for quality in (90, 75, 60, 40, 20):
        out = io.BytesIO()
        canvas.save(out, "WEBP", quality=quality)
        if out.tell() <= MAX_STICKER_BYTES:
            return out.getvalue()
    raise StickerError("The sticker doesn't fit in Signal's size limit")


@dataclass(frozen=True)
class PackRef:
    pack_id: str  # hex
    pack_key: str  # hex

    @property
    def url(self) -> str:
        return f"https://signal.art/addstickers/#pack_id={self.pack_id}&pack_key={self.pack_key}"


class StickerUploadError(StickerError):
    pass


class StickerRateLimited(StickerUploadError):
    pass


def account_credentials(signal_cli_dir: Path, number: str) -> tuple[str, str]:
    """The Signal service username and password of an account in signal-cli's data directory.

    accounts.json maps numbers to account files, which hold the password and device id.
    Linked devices log in as "<aci>.<device id>", the primary device as "<aci>".
    """
    data = signal_cli_dir / "data"
    try:
        accounts = json.loads((data / "accounts.json").read_text())["accounts"]
        entry = next(a for a in accounts if a.get("number") == number)
        if entry.get("environment", "LIVE") != "LIVE":
            raise StickerUploadError(f"Account {number} isn't on Signal's live servers")
        account = json.loads((data / entry["path"]).read_text())
        aci, password = entry["uuid"], account["password"]
        device_id = int(account.get("deviceId", 1))
    except StopIteration:
        raise StickerUploadError(f"signal-cli has no account {number}") from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise StickerUploadError(f"Can't read the signal-cli account: {type(e).__name__}: {e}") from e
    if not isinstance(aci, str) or not isinstance(password, str):
        raise StickerUploadError("The signal-cli account has no credentials")
    return (aci if device_id == 1 else f"{aci}.{device_id}"), password


class StickerPackUploader:
    """Uploads new sticker packs as the bot's account."""

    def __init__(self, signal_cli_dir: str, number: str, timeout: float = 60):
        self.signal_cli_dir = Path(signal_cli_dir)
        self.number = number
        # Signal's servers use Signal's own certificate authority, see stickers.py.
        verify = ssl.create_default_context(cadata=SIGNAL_CA)
        self._service = httpx.AsyncClient(base_url=SIGNAL_SERVICE, verify=verify, timeout=timeout)
        self._cdn = httpx.AsyncClient(base_url=SIGNAL_CDN, verify=verify, timeout=timeout)

    async def close(self) -> None:
        await self._service.aclose()
        await self._cdn.aclose()

    async def upload(self, stickers: list[PackSticker]) -> PackRef:
        """Upload a pack with these stickers. The first is also the cover. Raises StickerUploadError."""
        if not 1 <= len(stickers) <= MAX_STICKERS:
            raise StickerUploadError(f"A pack needs 1 to {MAX_STICKERS} stickers, not {len(stickers)}")
        credentials = account_credentials(self.signal_cli_dir, self.number)
        pack_key = os.urandom(32)
        try:
            # The server picks the pack id and signs one upload form per file.
            resp = await self._service.get(f"/v1/sticker/pack/form/{len(stickers)}", auth=credentials)
            if resp.status_code in (413, 429):
                raise StickerRateLimited("Signal's sticker pack rate limit")
            resp.raise_for_status()
            form = resp.json()
            pack_id, manifest_form, sticker_forms = str(form["packId"]), form["manifest"], form["stickers"]
            ids = sorted(int(f["id"]) for f in sticker_forms)
            if not re.fullmatch(r"[0-9a-f]{32}", pack_id) or ids != list(range(len(stickers))):
                raise StickerUploadError("Signal sent an unexpected upload form")
            manifest = build_manifest(PACK_TITLE, PACK_AUTHOR, [s.emoji for s in stickers], STICKER_CONTENT_TYPE)
            slots = asyncio.Semaphore(UPLOAD_CONCURRENCY)

            async def put(upload_form: dict, data: bytes) -> None:
                async with slots:
                    await self._put(upload_form, encrypt(data, pack_key))

            await put(manifest_form, manifest)
            # Each form names its sticker id, which is the sticker's place in the manifest.
            await asyncio.gather(*(put(f, stickers[int(f["id"])].image) for f in sticker_forms))
        except httpx.HTTPError as e:
            raise StickerUploadError(f"Sticker pack upload failed: {type(e).__name__}: {e}") from e
        except (ValueError, KeyError, TypeError) as e:
            raise StickerUploadError(f"Signal sent an unexpected upload form: {type(e).__name__}: {e}") from e
        return PackRef(pack_id, pack_key.hex())

    async def _put(self, upload_form: dict, data: bytes) -> None:
        """POST a file to the CDN with the signed S3 form Signal handed out for it."""
        fields = {
            "key": upload_form["key"],
            "x-amz-credential": upload_form["credential"],
            "acl": upload_form["acl"],
            "x-amz-algorithm": upload_form["algorithm"],
            "x-amz-date": upload_form["date"],
            "policy": upload_form["policy"],
            "x-amz-signature": upload_form["signature"],
            "Content-Type": "application/octet-stream",
        }
        # httpx sends the fields first and the file last, which is the order S3 needs.
        resp = await self._cdn.post("/", data=fields, files={"file": ("file", data, "application/octet-stream")})
        resp.raise_for_status()
