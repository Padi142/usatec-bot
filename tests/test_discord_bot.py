import asyncio
import json
from contextlib import suppress
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from tldr_bot.config import DiscordConfig
from tldr_bot.discord_bot import DiscordCatBot
from tldr_bot.health import heartbeat, is_healthy


@pytest.fixture
async def bot():
    async with DiscordCatBot(DiscordConfig("test-token", "test-key")) as client:
        yield client


@pytest.fixture
def interaction():
    value = Mock(spec=discord.Interaction)
    value.response = Mock(spec=discord.InteractionResponse)
    value.response.defer = AsyncMock()
    value.response.send_message = AsyncMock()
    value.edit_original_response = AsyncMock()
    value.filesize_limit = 10_000_000
    return value


def test_discord_config_needs_only_discord_and_cat_credentials(monkeypatch):
    for name in ("SIGNAL_NUMBER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "DISCORD_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("CAT_GEN_API_KEY", "key")
    assert DiscordConfig.from_env() == DiscordConfig("token", "key")
    monkeypatch.setenv("DISCORD_GUILD_ID", "12345")
    assert DiscordConfig.from_env().guild_id == 12345


async def test_slash_command_options(bot):
    commands = {command.name: command.to_dict(bot.tree) for command in bot.tree.get_commands()}
    assert set(commands) == {"macka", "mackagen"}
    options = {option["name"]: option for option in commands["mackagen"]["options"]}
    assert set(options) == {"prompt", "model", "layout", "seed"}
    assert options["prompt"]["max_length"] == 500
    assert options["seed"]["min_value"] == 0
    assert options["seed"]["max_value"] == 2**32 - 1
    assert [choice["value"] for choice in options["layout"]["choices"]] == [
        "single", "2x2", "3x3", "4x4", "5x5",
    ]
    assert not bot.intents.message_content


@pytest.mark.parametrize("guild_id", [None, 12345])
async def test_sync_commands_on_startup(bot, guild_id):
    bot.cfg = DiscordConfig("token", "key", guild_id)
    bot.tree.sync = AsyncMock(return_value=[])
    await bot.setup_hook()
    if guild_id:
        bot.tree.sync.assert_awaited_once_with(guild=discord.Object(id=guild_id))
        assert {command.name for command in bot.tree.get_commands(guild=discord.Object(id=guild_id))} == {
            "macka", "mackagen",
        }
    else:
        bot.tree.sync.assert_awaited_once_with()


@pytest.mark.parametrize("options, expected", [
    ({}, {"model": "cat-diffusion-v2", "layout": "single"}),
    ({"prompt": "a ginger cat"}, {"model": "flowmatch-cats-tti", "prompt": "a ginger cat", "layout": "single"}),
    ({"layout": "4x4", "seed": 0}, {"model": "cat-diffusion-v2", "layout": "4x4", "seed": 0}),
    ({"model": "cat-diffusion", "layout": "5x5", "seed": 2**32 - 1},
     {"model": "cat-diffusion", "layout": "5x5", "seed": 2**32 - 1}),
])
async def test_generate_through_registered_command(bot, interaction, options, expected):
    requests = []
    uploads = []

    def handler(request):
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        requests.append(request)
        if request.url.host == "images.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(202, json={
            "id": "j1", "state": "done", "images": [{"url": "https://images.test/cat.png"}],
        })

    async def capture_upload(**kwargs):
        attachment, = kwargs["attachments"]
        uploads.append((attachment.filename, attachment.fp.read()))

    bot.cat_gen._http._transport = httpx.MockTransport(handler)
    interaction.edit_original_response.side_effect = capture_upload
    await bot.tree.get_command("mackagen")._do_call(interaction, options)
    assert json.loads(requests[0].content) == expected
    assert uploads == [("cat.png", b"PNG")]


async def test_random_cat_through_registered_command(bot, interaction):
    bot.cats._http._transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"JPEG", headers={"content-type": "image/jpeg"},
    ))
    await bot.tree.get_command("macka")._do_call(interaction, {})
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    attachment, = interaction.edit_original_response.call_args.kwargs["attachments"]
    assert attachment.filename == "cat.jpg"


async def test_invalid_model_combination_does_not_submit(bot, interaction):
    requests = []
    bot.cat_gen._http._transport = httpx.MockTransport(lambda request: requests.append(request))
    await bot.tree.get_command("mackagen")._do_call(interaction, {"prompt": "a cat", "layout": "5x5"})
    assert not requests
    assert "Grids only work without a prompt" in interaction.edit_original_response.call_args.kwargs["content"]


async def test_generation_failure_finishes_deferred_response(bot, interaction):
    bot.cat_gen._http._transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    await bot.tree.get_command("mackagen")._do_call(interaction, {})
    interaction.response.defer.assert_awaited_once()
    assert "couldn't generate" in interaction.edit_original_response.call_args.kwargs["content"]


async def test_oversized_image_has_clear_error(bot, interaction):
    interaction.filesize_limit = 2
    await bot.send_image(interaction, "image/png", b"PNG")
    assert interaction.edit_original_response.call_args.kwargs == {
        "content": "This image exceeds Discord's upload limit.",
    }


async def test_discord_health_tracks_gateway_disconnect_and_reconnect(bot, tmp_path):
    path = tmp_path / "healthy"
    bot.is_ready = Mock(return_value=True)
    bot.ws = Mock(open=True, close=AsyncMock())
    task = asyncio.create_task(heartbeat(bot, path, interval=0))
    try:
        await asyncio.sleep(0)
        assert is_healthy(path)

        # discord.py keeps its ready flag set during a gateway outage.
        bot.ws.open = False
        await asyncio.sleep(0)
        assert not is_healthy(path)

        bot.ws.open = True
        await asyncio.sleep(0)
        assert is_healthy(path)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
