"""Signal sticker packs: their encryption and manifest format, and the emoji and picture of stickers.

signal-cli knows which emoji each sticker stands for, but its JSON output and the
REST API leave it out. The API does list every pack the account has seen, with
the pack key, so the bot fetches and decrypts the manifest, and the sticker's
picture when it gets described, itself.
"""

import asyncio
import hashlib
import hmac
import io
import logging
import os
import ssl
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
from PIL import Image
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .signal_api import SignalClient, Sticker

log = logging.getLogger(__name__)

SIGNAL_CDN = "https://cdn.signal.org"
# Signal's servers use Signal's own certificate authority, so it has to be trusted
# explicitly. From config/default.json in github.com/signalapp/Signal-Desktop.
SIGNAL_CA = """\
-----BEGIN CERTIFICATE-----
MIIF2zCCA8OgAwIBAgIUAMHz4g60cIDBpPr1gyZ/JDaaPpcwDQYJKoZIhvcNAQEL
BQAwdTELMAkGA1UEBhMCVVMxEzARBgNVBAgTCkNhbGlmb3JuaWExFjAUBgNVBAcT
DU1vdW50YWluIFZpZXcxHjAcBgNVBAoTFVNpZ25hbCBNZXNzZW5nZXIsIExMQzEZ
MBcGA1UEAxMQU2lnbmFsIE1lc3NlbmdlcjAeFw0yMjAxMjYwMDQ1NTFaFw0zMjAx
MjQwMDQ1NTBaMHUxCzAJBgNVBAYTAlVTMRMwEQYDVQQIEwpDYWxpZm9ybmlhMRYw
FAYDVQQHEw1Nb3VudGFpbiBWaWV3MR4wHAYDVQQKExVTaWduYWwgTWVzc2VuZ2Vy
LCBMTEMxGTAXBgNVBAMTEFNpZ25hbCBNZXNzZW5nZXIwggIiMA0GCSqGSIb3DQEB
AQUAA4ICDwAwggIKAoICAQDEecifxMHHlDhxbERVdErOhGsLO08PUdNkATjZ1kT5
1uPf5JPiRbus9F4J/GgBQ4ANSAjIDZuFY0WOvG/i0qvxthpW70ocp8IjkiWTNiA8
1zQNQdCiWbGDU4B1sLi2o4JgJMweSkQFiyDynqWgHpw+KmvytCzRWnvrrptIfE4G
PxNOsAtXFbVH++8JO42IaKRVlbfpe/lUHbjiYmIpQroZPGPY4Oql8KM3o39ObPnT
o1WoM4moyOOZpU3lV1awftvWBx1sbTBL02sQWfHRxgNVF+Pj0fdDMMFdFJobArrL
VfK2Ua+dYN4pV5XIxzVarSRW73CXqQ+2qloPW/ynpa3gRtYeGWV4jl7eD0PmeHpK
OY78idP4H1jfAv0TAVeKpuB5ZFZ2szcySxrQa8d7FIf0kNJe9gIRjbQ+XrvnN+ZZ
vj6d+8uBJq8LfQaFhlVfI0/aIdggScapR7w8oLpvdflUWqcTLeXVNLVrg15cEDwd
lV8PVscT/KT0bfNzKI80qBq8LyRmauAqP0CDjayYGb2UAabnhefgmRY6aBE5mXxd
byAEzzCS3vDxjeTD8v8nbDq+SD6lJi0i7jgwEfNDhe9XK50baK15Udc8Cr/ZlhGM
jNmWqBd0jIpaZm1rzWA0k4VwXtDwpBXSz8oBFshiXs3FD6jHY2IhOR3ppbyd4qRU
pwIDAQABo2MwYTAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zAdBgNV
HQ4EFgQUtfNLxuXWS9DlgGuMUMNnW7yx83EwHwYDVR0jBBgwFoAUtfNLxuXWS9Dl
gGuMUMNnW7yx83EwDQYJKoZIhvcNAQELBQADggIBABUeiryS0qjykBN75aoHO9bV
PrrX+DSJIB9V2YzkFVyh/io65QJMG8naWVGOSpVRwUwhZVKh3JVp/miPgzTGAo7z
hrDIoXc+ih7orAMb19qol/2Ha8OZLa75LojJNRbZoCR5C+gM8C+spMLjFf9k3JVx
dajhtRUcR0zYhwsBS7qZ5Me0d6gRXD0ZiSbadMMxSw6KfKk3ePmPb9gX+MRTS63c
8mLzVYB/3fe/bkpq4RUwzUHvoZf+SUD7NzSQRQQMfvAHlxk11TVNxScYPtxXDyiy
3Cssl9gWrrWqQ/omuHipoH62J7h8KAYbr6oEIq+Czuenc3eCIBGBBfvCpuFOgckA
XXE4MlBasEU0MO66GrTCgMt9bAmSw3TrRP12+ZUFxYNtqWluRU8JWQ4FCCPcz9pg
MRBOgn4lTxDZG+I47OKNuSRjFEP94cdgxd3H/5BK7WHUz1tAGQ4BgepSXgmjzifF
T5FVTDTl3ZnWUVBXiHYtbOBgLiSIkbqGMCLtrBtFIeQ7RRTb3L+IE9R0UB0cJB3A
Xbf1lVkOcmrdu2h8A32aCwtr5S1fBF1unlG7imPmqJfpOMWa8yIF/KWVm29JAPq8
Lrsybb0z5gg8w7ZblEuB9zOW9M3l60DXuJO6l7g+deV6P96rv2unHS8UlvWiVWDy
9qfgAJizyy3kqM4lOwBH
-----END CERTIFICATE-----
"""
# Signal allows 300 KiB per sticker; manifests are far smaller. More than this isn't a sticker.
MAX_STICKER_DOWNLOAD = 1_000_000
# Stickers are 512x512. This bounds the memory a hostile pack's picture can take to decode.
MAX_STICKER_PIXELS = 4_000_000
# signal-cli may still be saving the pack key when the message event arrives.
PACK_KEY_RETRY_DELAY = 2.0

_MAC_SIZE = 32
_IV_SIZE = 16


class StickerError(Exception):
    pass


def _hkdf(key: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 with an all-zero salt, as Signal derives sticker keys."""
    prk = hmac.new(bytes(32), key, hashlib.sha256).digest()
    output, block = b"", b""
    for counter in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
    return output[:length]


def _pack_keys(pack_key: bytes) -> tuple[bytes, bytes]:
    """The AES and HMAC keys that encrypt everything in a pack: its manifest and each sticker."""
    keys = _hkdf(pack_key, b"Sticker Pack", 64)
    return keys[:32], keys[32:]


def encrypt(data: bytes, pack_key: bytes) -> bytes:
    """Encrypt a manifest or sticker for the CDN: IV, AES-256-CBC ciphertext, then HMAC-SHA256 of both."""
    aes_key, mac_key = _pack_keys(pack_key)
    iv = os.urandom(_IV_SIZE)
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    body = iv + encryptor.update(padder.update(data) + padder.finalize()) + encryptor.finalize()
    return body + hmac.new(mac_key, body, hashlib.sha256).digest()


def decrypt(data: bytes, pack_key: bytes) -> bytes:
    """Decrypt a manifest.proto or a sticker: AES-256-CBC with a trailing HMAC-SHA256 over the IV and ciphertext."""
    aes_key, mac_key = _pack_keys(pack_key)
    body, mac = data[:-_MAC_SIZE], data[-_MAC_SIZE:]
    if len(body) <= _IV_SIZE or not hmac.compare_digest(hmac.new(mac_key, body, hashlib.sha256).digest(), mac):
        raise StickerError("Sticker data failed its integrity check")
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(body[:_IV_SIZE])).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        padded = decryptor.update(body[_IV_SIZE:]) + decryptor.finalize()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as e:
        raise StickerError(f"Sticker data is malformed: {e}") from e


def first_frame_png(data: bytes) -> bytes:
    """A sticker's picture as a PNG, so a vision model can read it. Animated ones give their first frame.

    Raises StickerError for what Pillow can't read, like Lottie animations.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_STICKER_PIXELS:
                raise StickerError(f"Sticker is {image.width}x{image.height}, too big to describe")
            out = io.BytesIO()
            image.convert("RGBA").save(out, "PNG")  # converting reads the first frame only
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise StickerError(f"Sticker isn't a readable picture: {e}") from e


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, pos


def _fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    """The (field number, value) pairs of a protobuf message, enough to read a manifest."""
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        match key & 7:
            case 0:
                value, pos = _varint(data, pos)
                yield key >> 3, value
            case 2:
                size, pos = _varint(data, pos)
                yield key >> 3, data[pos : pos + size]
                pos += size
            case 1:
                pos += 8
            case 5:
                pos += 4
            case wire_type:
                raise StickerError(f"Unsupported protobuf wire type {wire_type}")


def parse_emojis(manifest: bytes) -> dict[int, str]:
    """Sticker id to emoji, from a decrypted manifest.

    The manifest is Pack { title = 1; author = 2; cover = 3; repeated stickers = 4 },
    with each Sticker { id = 1; emoji = 2; contentType = 3 }.
    """
    emojis: dict[int, str] = {}
    try:
        for number, value in _fields(manifest):
            if number not in (3, 4) or not isinstance(value, bytes):
                continue
            sticker = dict(_fields(value))
            sticker_id, emoji = sticker.get(1, 0), sticker.get(2)
            if isinstance(sticker_id, int) and isinstance(emoji, bytes) and emoji:
                emojis[sticker_id] = emoji.decode()
    except (IndexError, UnicodeDecodeError) as e:
        raise StickerError(f"Sticker manifest is malformed: {e}") from e
    return emojis


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _encode_field(number: int, value: int | bytes) -> bytes:
    if isinstance(value, int):
        return _encode_varint(number << 3) + _encode_varint(value)
    return _encode_varint(number << 3 | 2) + _encode_varint(len(value)) + value


def _encode_sticker(sticker_id: int, emoji: str, content_type: str | None = None) -> bytes:
    fields = _encode_field(1, sticker_id) + _encode_field(2, emoji.encode())
    return fields + (_encode_field(3, content_type.encode()) if content_type else b"")


def build_manifest(title: str, author: str, emojis: list[str], content_type: str) -> bytes:
    """A manifest.proto for stickers 0..n-1 with these emojis. The first sticker is the cover."""
    pack = _encode_field(1, title.encode()) + _encode_field(2, author.encode())
    pack += _encode_field(3, _encode_sticker(0, ""))
    for sticker_id, emoji in enumerate(emojis):
        pack += _encode_field(4, _encode_sticker(sticker_id, emoji, content_type))
    return pack


@dataclass(frozen=True)
class _Pack:
    key: bytes
    emojis: dict[int, str]  # sticker id to emoji


class Stickers:
    """Reads the stickers people send: their emoji and their picture.

    Each pack's key and manifest are fetched once, then kept in memory.
    """

    def __init__(self, signal: SignalClient):
        self._signal = signal
        self._cdn = httpx.AsyncClient(
            base_url=SIGNAL_CDN, verify=ssl.create_default_context(cadata=SIGNAL_CA), timeout=15,
        )
        self._packs: dict[str, _Pack] = {}

    async def close(self) -> None:
        await self._cdn.aclose()

    async def emoji(self, sticker: Sticker) -> str | None:
        """The sticker's emoji, or None if its pack doesn't name one. Raises StickerError."""
        return (await self._pack(sticker.pack_id)).emojis.get(sticker.sticker_id)

    async def picture(self, sticker: Sticker) -> bytes:
        """The sticker's picture as a PNG. Raises StickerError."""
        pack = await self._pack(sticker.pack_id)
        data = await self._download(f"/stickers/{sticker.pack_id}/full/{sticker.sticker_id}", MAX_STICKER_DOWNLOAD)
        return first_frame_png(decrypt(data, pack.key))

    async def _pack(self, pack_id: str) -> _Pack:
        if pack_id not in self._packs:
            self._packs[pack_id] = await self._load_pack(pack_id)
        return self._packs[pack_id]

    async def _load_pack(self, pack_id: str) -> _Pack:
        try:
            pack_key = await self._signal.sticker_pack_key(pack_id)
            if pack_key is None:
                await asyncio.sleep(PACK_KEY_RETRY_DELAY)
                pack_key = await self._signal.sticker_pack_key(pack_id)
        except httpx.HTTPError as e:
            raise StickerError(f"Sticker pack {pack_id} key lookup failed: {e}") from e
        if pack_key is None:
            raise StickerError(f"Signal doesn't know the key of sticker pack {pack_id}")
        try:
            key = bytes.fromhex(pack_key)
        except ValueError as e:
            raise StickerError(f"Sticker pack {pack_id} has an invalid key") from e
        manifest = await self._download(f"/stickers/{pack_id}/manifest.proto", MAX_STICKER_DOWNLOAD)
        return _Pack(key, parse_emojis(decrypt(manifest, key)))

    async def _download(self, path: str, max_bytes: int) -> bytes:
        """A file from Signal's CDN, read no further than max_bytes. Raises StickerError."""
        try:
            async with self._cdn.stream("GET", path) as resp:
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > max_bytes:
                        raise StickerError(f"{path} is over {max_bytes} bytes")
                return bytes(data)
        except httpx.HTTPError as e:
            raise StickerError(f"{path} download failed: {e}") from e
