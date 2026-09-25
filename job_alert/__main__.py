import asyncio
import logging
import sys

import httpx

from .app import JobAlertBot
from .config import ConfigError, load_config
from .db import Store


async def main() -> None:
    config = load_config()
    store = Store(config.db_path)
    try:
        async with httpx.AsyncClient() as http:
            await JobAlertBot(config, store, http).run()
    finally:
        store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
