import asyncio
import logging
import os

from .bot import TldrBot
from .cats import CatClient, CatGenClient
from .config import Config
from .llm import LLMClient
from .signal_api import SignalClient
from .store import MessageStore


async def main() -> None:
    cfg = Config.from_env()
    store = MessageStore(cfg.db_path, cfg.history_limit)
    signal = SignalClient(cfg.signal_api_url, cfg.signal_number)
    llm = LLMClient(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key, cfg.llm_timeout)
    cats = CatClient()
    cat_gen = CatGenClient(cfg.cat_gen_api_key) if cfg.cat_gen_api_key else None
    try:
        await TldrBot(cfg, store, signal, llm, cats, cat_gen).run()
    finally:
        await signal.close()
        await llm.close()
        await cats.close()
        if cat_gen:
            await cat_gen.close()
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
