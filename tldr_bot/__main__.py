import asyncio
import logging
import os

from .bot import TldrBot
from .cats import CatClient, CatGenClient
from .config import Config
from .features import GitHubClient
from .health import heartbeat
from .llm import LLMClient
from .signal_api import SignalClient
from .sticker_pack import StickerPackUploader
from .stickers import Stickers
from .store import MessageStore


async def main() -> None:
    cfg = Config.from_env()
    store = MessageStore(cfg.db_path, cfg.history_limit)
    signal = SignalClient(cfg.signal_api_url, cfg.signal_number)
    llm = LLMClient(cfg.llm_base_url, cfg.llm_api_key, cfg.llm_timeout)
    cats = CatClient()
    cat_gen = CatGenClient(cfg.cat_gen_api_key) if cfg.cat_gen_api_key else None
    stickers = Stickers(signal)
    github = GitHubClient(cfg.github_token, cfg.github_repo)
    packs = StickerPackUploader(cfg.signal_cli_dir, cfg.signal_number) if cfg.signal_cli_dir else None
    health = asyncio.create_task(heartbeat(signal))
    try:
        await TldrBot(cfg, store, signal, llm, cats, cat_gen, stickers, github, packs).run()
    finally:
        health.cancel()
        await signal.close()
        await llm.close()
        await cats.close()
        await stickers.close()
        if cat_gen:
            await cat_gen.close()
        await github.close()
        if packs:
            await packs.close()
        store.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
