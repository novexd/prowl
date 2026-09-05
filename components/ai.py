import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import os
import time
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db


async def _resolve_key(guild_id):
    """Pick the active API key: per-guild first (no rate limit), then global DB, then env."""
    own = None
    # Per-guild key (stored in ai_settings)
    try:
        s = await get_ai_settings(int(guild_id))
        keys = s.get("api_keys", {})
        if isinstance(keys, dict):
            for name in ("openrouter", "groq", "openai"):
                if keys.get(name):
                    return keys[name], name, True  # own key - no rate limit
    except Exception:
        pass
    # Global/admin key from DB
    for name in ("openrouter", "groq", "openai"):
        try:
            pool = await neon_db.get_pool()
            if pool:
                row = await pool.fetchrow("SELECT value FROM api_keys WHERE key_name = ?", name)
                if row and row["value"]:
                    return row["value"], name, False
        except Exception:
            pass
        env = os.environ.get(f"{name.upper()}_API_KEY", "")
        if env:
            return env, name, False
    return "", "", False


async def _resolve_base(provider):
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider == "groq":
        return "https://api.groq.com/openai/v1"
    return "https://api.openai.com/v1"


AI_MODELS = [
    # OpenAI
    ("gpt-4o (OpenAI)", "gpt-4o"),
    ("gpt-4o-mini (OpenAI)", "gpt-4o-mini"),
    ("gpt-4-turbo (OpenAI)", "gpt-4-turbo"),
    ("gpt-4 (OpenAI)", "gpt-4"),
    ("gpt-3.5-turbo (OpenAI)", "gpt-3.5-turbo"),
    ("o1-preview (OpenAI)", "o1-preview"),
    ("o1-mini (OpenAI)", "o1-mini"),
    # Groq
    ("Llama 3.3 70B (Groq)", "llama-3.3-70b-versatile"),
    ("Llama 3.1 8B (Groq)", "llama-3.1-8b-instant"),
    ("Gemma 2 9B (Groq)", "gemma2-9b-it"),
    ("Mixtral 8x7B (Groq)", "mixtral-8x7b-32768"),
    ("Llama3 8B Tool Use (Groq)", "llama3-groq-8b-8192-tool-use-preview"),
    ("Llama3 70B Tool Use (Groq)", "llama3-groq-70b-8192-tool-use-preview"),
    # OpenRouter
    ("Claude 3.5 Sonnet (OpenRouter)", "anthropic/claude-3.5-sonnet"),
    ("Claude 3 Haiku (OpenRouter)", "anthropic/claude-3-haiku"),
    ("Gemini 2.0 Flash (OpenRouter)", "google/gemini-2.0-flash-exp:free"),
    ("Gemini Pro 1.5 (OpenRouter)", "google/gemini-pro-1.5"),
    ("Llama 3.1 70B (OpenRouter)", "meta-llama/llama-3.1-70b-instruct"),
    ("GPT-4o (OpenRouter)", "openai/gpt-4o"),
    ("GPT-4o Mini (OpenRouter)", "openai/gpt-4o-mini"),
    ("Mistral Large (OpenRouter)", "mistralai/mistral-large-latest"),
    ("DeepSeek Chat (OpenRouter)", "deepseek/deepseek-chat"),
    ("Command R+ (OpenRouter)", "cohere/command-r-plus"),
    ("Qwen 2.5 72B (OpenRouter)", "qwen/qwen-2.5-72b-instruct"),
    ("Phi 3 Medium (OpenRouter)", "microsoft/phi-3-medium-128k-instruct"),
]

AI_DEFAULTS = {
    "enabled": True,
    "model": "gpt-3.5-turbo",
    "system_prompt": "You are a helpful Discord bot named Prowl. Be concise and friendly.",
    "max_tokens": 500,
    "temperature": 0.7,
}

IMAGE_MODELS = [
    ("DALL-E 3 (OpenAI)", "dall-e-3"),
    ("DALL-E 2 (OpenAI)", "dall-e-2"),
]


async def _autocomplete_models(interaction: discord.Interaction, current: str):
    current_lower = current.lower()
    return [
        app_commands.Choice(name=name, value=value)
        for name, value in AI_MODELS
        if current_lower in name.lower() or current_lower in value.lower()
    ][:25]


async def _autocomplete_image_models(interaction: discord.Interaction, current: str):
    current_lower = current.lower()
    return [
        app_commands.Choice(name=name, value=value)
        for name, value in IMAGE_MODELS
        if current_lower in name.lower() or current_lower in value.lower()
    ][:25]


async def get_ai_settings(guild_id: int):
    return await neon_db.load_cached_settings("ai_settings", guild_id, AI_DEFAULTS)


async def save_ai_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("ai_settings", guild_id, settings)


class AI(commands.Cog, name="AI"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions = {}
        self._last_global = 0
        self._last_user = {}

    ai_group = app_commands.Group(name="ai", description="AI-powered features")

    @ai_group.command(name="chat", description="Chat with the AI")
    @app_commands.describe(prompt="What you want to say to the AI")
    async def chat(self, interaction: discord.Interaction, prompt: str):
        api_key, provider, own_key = await _resolve_key(str(interaction.guild_id))
        if not api_key:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "AI Not Configured")).description("No API key set. Contact the bot owner.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

        # Rate limits: only when using the global bot key (not the server's own key)
        now = time.time()
        if not own_key:
            if now - self._last_global < 5:
                wait = int(5 - (now - self._last_global))
                return await interaction.response.send_message(
                    embed=EmbedBuilder().title(emoji_title("warning", "Slow Down")).description(f"Global cooldown - try again in {wait}s. Provide your own AI key on the dashboard to skip rate limits.").color("warn").timestamp(datetime.datetime.utcnow()).build(),
                    ephemeral=True
                )
            uid = str(interaction.user.id)
            last = self._last_user.get(uid, 0)
            if now - last < 60:
                wait = int(60 - (now - last))
                return await interaction.response.send_message(
                    embed=EmbedBuilder().title(emoji_title("warning", "Cooldown")).description(f"You can use AI again in {wait}s. Provide your own AI key on the dashboard to skip rate limits.").color("warn").timestamp(datetime.datetime.utcnow()).build(),
                    ephemeral=True
                )
            self._last_global = now
            self._last_user[uid] = now

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        if guild_id not in self.sessions:
            self.sessions[guild_id] = []

        settings = await get_ai_settings(interaction.guild_id)
        system_prompt = settings.get("system_prompt", AI_DEFAULTS["system_prompt"])
        model = settings.get("model", "gpt-3.5-turbo")
        max_tokens = settings.get("max_tokens", 500)
        temperature = settings.get("temperature", 0.7)

        self.sessions[guild_id].append({"role": "user", "content": prompt})
        messages = [{"role": "system", "content": system_prompt}] + self.sessions[guild_id][-20:]

        try:
            api_base = await _resolve_base(provider)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                ) as resp:
                    if resp.status != 200:
                        error_data = await resp.json()
                        error_msg = error_data.get("error", {}).get("message", "Unknown error")
                        return await interaction.followup.send(
                            embed=EmbedBuilder().title(emoji_title("error", "AI Error")).description(f"API returned error: {error_msg[:200]}").color("error").timestamp(datetime.datetime.utcnow()).build(),
                            ephemeral=True
                        )
                    data = await resp.json()
                    reply = data["choices"][0]["message"]["content"]
                    tokens_used = data.get("usage", {}).get("total_tokens", 0)
                    self.sessions[guild_id].append({"role": "assistant", "content": reply})
                    await interaction.followup.send(
                        content=f"{interaction.user.mention}\n\n{reply[:4000]}",
                        embed=EmbedBuilder()
                        .color("blue")
                        .row(
                            ('Model', model),
                            ('Tokens Used', str(tokens_used))
                        )
                        .footer(f"Requested by {interaction.user.display_name}")
                        .timestamp(datetime.datetime.utcnow())
                        .build(),
                        ephemeral=True,
                    )
        except aiohttp.ClientError as e:
            await interaction.followup.send(
                embed=EmbedBuilder().title(emoji_title("error", "Connection Error")).description(f"Failed to reach AI service: {str(e)[:200]}").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"AI chat error: {e}")
            await interaction.followup.send(
                embed=EmbedBuilder().title(emoji_title("error", "AI Error")).description(f"Something went wrong: {str(e)[:200]}").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @ai_group.command(name="clear", description="Clear the AI conversation history")
    async def clear_history(self, interaction: discord.Interaction):
        self.sessions.pop(str(interaction.guild_id), None)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "History Cleared")).description("AI conversation history has been cleared.").color("success").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @ai_group.command(name="imagine", description="Generate an image from a text prompt")
    @app_commands.describe(
        prompt="Describe the image you want to generate",
        model="The image model to use"
    )
    @app_commands.autocomplete(model=_autocomplete_image_models)
    async def imagine(self, interaction: discord.Interaction, prompt: str, model: str = "dall-e-3"):
        api_key, provider, _ = await _resolve_key(str(interaction.guild_id))
        if not api_key:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "AI Not Configured")).description("No API key set.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if provider == "groq":
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Supported")).description("Groq does not support image generation. Use an OpenAI or OpenRouter key.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await interaction.response.defer()
        try:
            api_base = await _resolve_base(provider)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{api_base}/images/generations",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "prompt": prompt, "n": 1, "size": "1024x1024"},
                ) as resp:
                    if resp.status != 200:
                        error_data = await resp.json()
                        error_msg = error_data.get("error", {}).get("message", "Unknown error")
                        return await interaction.followup.send(
                            embed=EmbedBuilder().title(emoji_title("error", "Generation Failed")).description(f"API error: {error_msg[:200]}").color("error").timestamp(datetime.datetime.utcnow()).build(),
                            ephemeral=True
                        )
                    data = await resp.json()
                    image_url = data["data"][0]["url"]
                    embed = (
                        EmbedBuilder()
                        .title(emoji_title("image", "Generated Image"))
                        .description(prompt[:1000])
                        .image(image_url)
                        .color("gray")
                        .row(
                            ('Model', f"`{model}` ({provider.title()})"),
                            ('Requested by', interaction.user.display_name),
                        )
                        .timestamp(datetime.datetime.utcnow())
                        .build()
                    )
                    await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"AI imagine error: {e}")
            await interaction.followup.send(
                embed=EmbedBuilder().title(emoji_title("error", "Generation Failed")).description(f"Something went wrong: {str(e)[:200]}").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @ai_group.command(name="config", description="View AI configuration")
    async def config(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_ai_settings(interaction.guild_id)
        embed = (
            EmbedBuilder()
            .title(emoji_title("settings", "AI Configuration"))
            .color("brand")
            .row(
                ('Enabled', 'Yes' if settings.get('enabled') else 'No'),
                ('Model', settings.get('model', 'gpt-3.5-turbo')),
                ('Max Tokens', str(settings.get('max_tokens', 500))),
                ('Temperature', str(settings.get('temperature', 0.7))),
                ('System Prompt', settings.get('system_prompt', AI_DEFAULTS['system_prompt'])[:1024])
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ai_group.command(name="model", description="Set the AI model to use")
    @app_commands.describe(model="The model name")
    @app_commands.autocomplete(model=_autocomplete_models)
    async def set_model(self, interaction: discord.Interaction, model: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_ai_settings(interaction.guild_id)
        settings["model"] = model
        await save_ai_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Model Updated")).description(f"AI model set to **{model}**").color("success").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @ai_group.command(name="prompt", description="Set the AI system prompt")
    @app_commands.describe(prompt="The system prompt for the AI")
    async def set_prompt(self, interaction: discord.Interaction, prompt: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if len(prompt) > 1000:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Too Long")).description("System prompt too long (max 1000 characters).").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_ai_settings(interaction.guild_id)
        settings["system_prompt"] = prompt
        await save_ai_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Prompt Updated")).description(f"System prompt updated:\n```\n{prompt[:500]}\n```").color("success").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
