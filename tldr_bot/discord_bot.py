"""Discord cat slash commands. Run with `python -m tldr_bot.discord_bot`."""

import asyncio
import io
import logging
import os
from contextlib import suppress

import discord
from discord import app_commands

from .cats import GRID_LAYOUTS, PROMPT_MAX_LENGTH, SEED_MAX, CatClient, CatError, CatGenClient
from .config import DiscordConfig
from .health import heartbeat

log = logging.getLogger(__name__)


class DiscordCatBot(discord.Client):
    def __init__(self, cfg: DiscordConfig):
        # Slash commands need no message content or privileged gateway intents.
        super().__init__(intents=discord.Intents(guilds=True), allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.cats = CatClient()
        self.cat_gen = CatGenClient(cfg.cat_gen_api_key)
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(app_commands.Command(
            name="macka", description="Get a random cat picture.", callback=self.random_cat,
        ))
        self.tree.add_command(app_commands.Command(
            name="mackagen", description="Generate a cat or a grid of cats.", callback=self.generate,
        ))
        self.tree.error(self.on_command_error)
        self._health_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        # is_ready() stays set while discord.py retries a disconnected gateway.
        return self.is_ready() and self.ws is not None and self.ws.open

    async def setup_hook(self) -> None:
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            commands = await self.tree.sync(guild=guild)
        else:
            commands = await self.tree.sync()
        log.info("Synced %d Discord slash commands", len(commands))
        self._health_task = asyncio.create_task(heartbeat(self))

    async def close(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._health_task
        try:
            await super().close()
        finally:
            await self.cats.close()
            await self.cat_gen.close()

    async def random_cat(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            content_type, image = await self.cats.random_cat()
        except CatError:
            log.exception("Discord cat download failed")
            await interaction.edit_original_response(content="I couldn't find a cat right now. Try again later.")
            return
        await self.send_image(interaction, content_type, image)

    @app_commands.describe(
        prompt="Describe your cat (text-to-cat only).",
        model="Automatic uses text-to-cat with a prompt, cat diffusion v2 otherwise.",
        layout="One picture, or a grid of cats (cat-diffusion models only).",
        seed="Use a fixed seed, or leave empty for a random one.",
    )
    @app_commands.choices(
        model=[
            app_commands.Choice(name="Automatic", value="auto"),
            app_commands.Choice(name="Cat diffusion v2", value="cat-diffusion-v2"),
            app_commands.Choice(name="Cat diffusion (original)", value="cat-diffusion"),
            app_commands.Choice(name="Text-to-cat", value="flowmatch-cats-tti"),
        ],
        layout=[
            app_commands.Choice(name="Single cat", value="single"),
            *[app_commands.Choice(name=size, value=size) for size in GRID_LAYOUTS],
        ],
    )
    async def generate(
        self,
        interaction: discord.Interaction,
        prompt: app_commands.Range[str, 1, PROMPT_MAX_LENGTH] | None = None,
        model: str = "auto",
        layout: str = "single",
        seed: app_commands.Range[int, 0, SEED_MAX] | None = None,
    ) -> None:
        # Acknowledge before waiting for the generation queue, then replace that response.
        await interaction.response.defer(thinking=True)
        try:
            content_type, image = await self.cat_gen.generate(
                prompt, layout, model=None if model == "auto" else model, seed=seed,
            )
        except ValueError as e:
            await interaction.edit_original_response(content=str(e))
            return
        except CatError:
            log.exception("Discord cat generation failed")
            await interaction.edit_original_response(content="I couldn't generate a cat right now. Try again later.")
            return
        await self.send_image(interaction, content_type, image)

    async def send_image(self, interaction: discord.Interaction, content_type: str, image: bytes) -> None:
        if len(image) > interaction.filesize_limit:
            await interaction.edit_original_response(content="This image exceeds Discord's upload limit.")
            return
        extension = {"image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(content_type, "png")
        with io.BytesIO(image) as image_file:
            attachment = discord.File(image_file, filename=f"cat.{extension}")
            try:
                await interaction.edit_original_response(content="🐈", attachments=[attachment])
            finally:
                attachment.close()

    async def on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("Discord slash command failed", exc_info=(type(error), error, error.__traceback__))
        message = "Something went wrong. Try again later."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=message)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def main() -> None:
    cfg = DiscordConfig.from_env()
    async with DiscordCatBot(cfg) as bot:
        await bot.start(cfg.token)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
