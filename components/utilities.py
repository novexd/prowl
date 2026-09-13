import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import io
import os
import shutil
import tempfile
import time
from typing import Optional

import aiohttp
import pyzipper
from PIL import Image

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title


IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "ico"}
IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    "tiff": "image/tiff", "ico": "image/x-icon",
}
AUDIO_FORMATS = {"mp3", "ogg", "wav", "flac", "m4a", "aac", "wma"}
AUDIO_MIME = {
    "mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav",
    "flac": "audio/flac", "m4a": "audio/mp4", "aac": "audio/aac",
    "wma": "audio/x-ms-wma",
}
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


def _ext_from_url(url: str) -> str:
    path = url.split("?")[0]
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def _is_image(filename: str, content_type: str = "") -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in IMAGE_FORMATS or "image" in content_type


def _is_audio(filename: str, content_type: str = "") -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in AUDIO_FORMATS or "audio" in content_type


class Utilities(commands.Cog):
    """Image and audio conversion, resize, and compression."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /convert ────────────────────────────────────────────────────

    @app_commands.command(name="convert", description="Convert an image or audio file to another format")
    @app_commands.describe(file="The file to convert", format="Target format (png, jpg, webp, mp3, ogg, etc.)")
    @app_commands.choices(format=[
        app_commands.Choice(name="PNG", value="png"),
        app_commands.Choice(name="JPG", value="jpg"),
        app_commands.Choice(name="WebP", value="webp"),
        app_commands.Choice(name="GIF", value="gif"),
        app_commands.Choice(name="BMP", value="bmp"),
        app_commands.Choice(name="MP3", value="mp3"),
        app_commands.Choice(name="OGG", value="ogg"),
        app_commands.Choice(name="WAV", value="wav"),
        app_commands.Choice(name="FLAC", value="flac"),
    ])
    async def convert_cmd(self, interaction: discord.Interaction, file: discord.Attachment, format: str):
        await interaction.response.defer(ephemeral=True)

        fmt = format.lower()
        filename = file.filename or "file"
        ct = file.content_type or ""

        # Image conversion
        if _is_image(filename, ct) and fmt in IMAGE_FORMATS:
            try:
                data = await file.read()
                img = Image.open(io.BytesIO(data))

                if fmt in ("jpg", "jpeg") and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                out = io.BytesIO()
                save_kwargs = {}
                if fmt == "png":
                    save_kwargs["optimize"] = True
                elif fmt in ("jpg", "jpeg"):
                    save_kwargs["quality"] = 95
                    save_kwargs["optimize"] = True
                elif fmt == "webp":
                    save_kwargs["quality"] = 90
                elif fmt == "gif" and img.mode != "RGBA":
                    img = img.convert("RGBA")

                img.save(out, format=fmt.upper().replace("JPG", "JPEG"), **save_kwargs)
                out.seek(0)

                if len(out.getvalue()) > 8_000_000:
                    return await interaction.followup.send("Result too large (>8MB). Try a smaller source or different format.", ephemeral=True)

                result_file = discord.File(out, filename=f"converted.{fmt}", spoiler=False)
                embed = (
                    EmbedBuilder()
                    .description(f"`{filename}` → `converted.{fmt}`").header(emoji_title("sparkle", "Image Converted"))
                    .color("pink")
                    .divider().row(("Size", f"{len(out.getvalue()) / 1024:.1f} KB"), ("Format", fmt.upper()))
                    .build()
                )
                await interaction.followup.send(embed=embed, file=result_file, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Conversion failed: `{e}`", ephemeral=True)

        # Audio conversion
        elif _is_audio(filename, ct) and fmt in AUDIO_FORMATS:
            if not FFMPEG_AVAILABLE:
                return await interaction.followup.send("Audio conversion is not available on this bot.", ephemeral=True)
            try:
                data = await file.read()
                if len(data) > 50_000_000:
                    return await interaction.followup.send("File too large (>50MB).", ephemeral=True)

                with tempfile.TemporaryDirectory() as tmpdir:
                    in_path = os.path.join(tmpdir, f"input.{_ext_from_url(filename) or 'bin'}")
                    out_path = os.path.join(tmpdir, f"output.{fmt}")
                    with open(in_path, "wb") as f:
                        f.write(data)

                    cmd = ["ffmpeg", "-y", "-i", in_path]
                    if fmt == "mp3":
                        cmd += ["-codec:a", "libmp3lame", "-q:a", "2"]
                    elif fmt == "ogg":
                        cmd += ["-codec:a", "libvorbis", "-q:a", "4"]
                    elif fmt == "wav":
                        cmd += ["-codec:a", "pcm_s16le"]
                    elif fmt == "flac":
                        cmd += ["-codec:a", "flac"]
                    cmd.append(out_path)

                    proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    _, stderr = await proc.communicate()

                    if proc.returncode != 0:
                        err = stderr.decode(errors="replace")[:500]
                        return await interaction.followup.send(f"FFmpeg error:\n```{err}```", ephemeral=True)

                    if not os.path.exists(out_path):
                        return await interaction.followup.send("Conversion produced no output.", ephemeral=True)

                    with open(out_path, "rb") as f:
                        out_data = f.read()

                    if len(out_data) > 8_000_000:
                        return await interaction.followup.send("Result too large (>8MB).", ephemeral=True)

                    result_file = discord.File(io.BytesIO(out_data), filename=f"converted.{fmt}")
                    embed = (
                        EmbedBuilder()
                        .description(f"`{filename}` → `converted.{fmt}`").header(emoji_title("music", "Audio Converted"))
                        .color("pink")
                        .divider().row(("Size", f"{len(out_data) / 1024:.1f} KB"), ("Format", fmt.upper()))
                        .build()
                    )
                    await interaction.followup.send(embed=embed, file=result_file, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Conversion failed: `{e}`", ephemeral=True)
        else:
            await interaction.followup.send(f"Cannot convert `{filename}` to `{fmt}`. Check the file type and target format.", ephemeral=True)

    # ── /resize ─────────────────────────────────────────────────────

    @app_commands.command(name="resize", description="Resize an image to specific dimensions")
    @app_commands.describe(file="The image to resize", width="New width (max 4096)", height="New height (max 4096)")
    async def resize_cmd(self, interaction: discord.Interaction, file: discord.Attachment, width: int, height: int):
        await interaction.response.defer(ephemeral=True)

        if width < 1 or width > 4096 or height < 1 or height > 4096:
            return await interaction.followup.send("Dimensions must be between 1 and 4096.", ephemeral=True)

        filename = file.filename or "image.png"
        ct = file.content_type or ""
        if not _is_image(filename, ct):
            return await interaction.followup.send("That's not an image file.", ephemeral=True)

        try:
            data = await file.read()
            img = Image.open(io.BytesIO(data))
            orig_w, orig_h = img.size

            img = img.resize((width, height), Image.LANCZOS)

            out = io.BytesIO()
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
            if ext in ("jpg", "jpeg") and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out, format=ext.upper().replace("JPG", "JPEG"), optimize=True)
            out.seek(0)

            if len(out.getvalue()) > 8_000_000:
                return await interaction.followup.send("Result too large (>8MB).", ephemeral=True)

            result_file = discord.File(out, filename=f"resized.{ext}")
            embed = (
                EmbedBuilder()
                .description(f"`{orig_w}×{orig_h}` → `{width}×{height}`").header(emoji_title("sparkle", "Image Resized"))
                .color("pink")
                .divider().row(("Original", f"{orig_w}×{orig_h}"), ("New", f"{width}×{height}"))
                .build()
            )
            await interaction.followup.send(embed=embed, file=result_file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Resize failed: `{e}`", ephemeral=True)

    # ── /compress ───────────────────────────────────────────────────

    @app_commands.command(name="compress", description="Compress an image to reduce file size")
    @app_commands.describe(file="The image to compress", quality="Quality 1-100 (default 60)")
    async def compress_cmd(self, interaction: discord.Interaction, file: discord.Attachment, quality: int = 60):
        await interaction.response.defer(ephemeral=True)

        if quality < 1 or quality > 100:
            return await interaction.followup.send("Quality must be between 1 and 100.", ephemeral=True)

        filename = file.filename or "image.png"
        ct = file.content_type or ""
        if not _is_image(filename, ct):
            return await interaction.followup.send("That's not an image file.", ephemeral=True)

        try:
            data = await file.read()
            orig_size = len(data)
            img = Image.open(io.BytesIO(data))

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"

            if ext in ("jpg", "jpeg"):
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=quality, optimize=True)
            elif ext == "webp":
                out = io.BytesIO()
                img.save(out, format="WEBP", quality=quality)
            elif ext == "png":
                out = io.BytesIO()
                if quality < 50:
                    img = img.quantize(colors=max(16, int(256 * quality / 50))).convert("RGB")
                    img.save(out, format="PNG", optimize=True)
                else:
                    img.save(out, format="PNG", optimize=True)
            elif ext == "gif":
                out = io.BytesIO()
                img.save(out, format="GIF", optimize=True)
            else:
                out = io.BytesIO()
                img.save(out, format=ext.upper().replace("JPG", "JPEG"), quality=quality)

            out.seek(0)
            new_size = len(out.getvalue())
            pct = ((orig_size - new_size) / orig_size * 100) if orig_size else 0

            if new_size > 8_000_000:
                return await interaction.followup.send("Compressed result is still too large (>8MB).", ephemeral=True)

            result_file = discord.File(out, filename=f"compressed.{ext}")
            embed = (
                EmbedBuilder()
                .description(f"Reduced by **{pct:.1f}%**").header(emoji_title("check", "Image Compressed"))
                .color("green")
                .divider().row(("Original", f"{orig_size / 1024:.1f} KB"), ("Compressed", f"{new_size / 1024:.1f} KB"))
                .build()
            )
            await interaction.followup.send(embed=embed, file=result_file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Compression failed: `{e}`", ephemeral=True)

    # ── /makezip ────────────────────────────────────────────────────

    LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
    MAX_ZIP_FILES = 10

    @app_commands.command(name="makezip", description="Create a zip archive from uploaded files (hosted for 24 hours)")
    @app_commands.describe(
        file1="First file to add",
        file2="Second file",
        file3="Third file",
        file4="Fourth file",
        file5="Fifth file",
        password="Optional password to encrypt the zip",
    )
    async def makezip_cmd(
        self,
        interaction: discord.Interaction,
        file1: discord.Attachment,
        file2: discord.Attachment = None,
        file3: discord.Attachment = None,
        file4: discord.Attachment = None,
        file5: discord.Attachment = None,
        password: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        files = [f for f in (file1, file2, file3, file4, file5) if f is not None]
        if len(files) > self.MAX_ZIP_FILES:
            return await interaction.followup.send(f"Too many files. Max {self.MAX_ZIP_FILES}.", ephemeral=True)

        total_size = sum(f.size or 0 for f in files)
        if total_size > 100_000_000:
            return await interaction.followup.send("Total file size exceeds 100MB limit.", ephemeral=True)

        try:
            zip_buf = io.BytesIO()
            with pyzipper.AESZipFile(zip_buf, "w", compression=pyzipper.ZIP_DEFLATED) as zf:
                if password:
                    zf.setpassword(password.encode("utf-8"))
                    zf.setencryption(pyzipper.WZ_AES, encrypt_force_zip64=True)
                names_seen = {}
                for f in files:
                    data = await f.read()
                    name = f.filename or "file"
                    if name in names_seen:
                        names_seen[name] += 1
                        name = f"{names_seen[name]}_{name}"
                    else:
                        names_seen[name] = 0
                    zf.writestr(name, data)

            zip_buf.seek(0)
            zip_data = zip_buf.read()

            if len(zip_data) > 1_000_000_000:
                return await interaction.followup.send("Zip file exceeds 1GB limit.", ephemeral=True)

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(zip_data)
                tmp_path = tmp.name

            try:
                async with aiohttp.ClientSession() as session:
                    form = aiohttp.FormData()
                    form.add_field("reqtype", "fileupload")
                    form.add_field("time", "24h")
                    form.add_field("fileToUpload", open(tmp_path, "rb"), filename="archive.zip", content_type="application/zip")
                    async with session.post(self.LITTERBOX_URL, data=form) as resp:
                        text = await resp.text()
                        if resp.status != 200 or not text.startswith("http"):
                            return await interaction.followup.send(f"Litterbox upload failed: `{text[:200]}`", ephemeral=True)
                        link = text.strip()
            finally:
                os.unlink(tmp_path)

            file_names = ", ".join(f"`{f.filename}`" for f in files[:5])
            embed = (
                EmbedBuilder()
                .description(f"**Files:** {file_names}\n**Expires:** 24 hours").header(emoji_title("package", "Zip Created"))
                .color("brand")
                .divider().row(
                    ("Size", f"{len(zip_data) / 1024:.1f} KB"),
                    ("Files", str(len(files))),
                    ("Encrypted", "Yes" if password else "No"),
                )
                .build()
            )
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Download", url=link, style=discord.ButtonStyle.link))
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to create zip: `{e}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utilities(bot))
