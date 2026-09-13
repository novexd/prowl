import discord
from discord import app_commands
from discord.ext import commands

from Ediscord.builders import (
    EmbedBuilder,
    ButtonBuilder,
    LinkBuilder,
    ModalBuilder,
    ButtonView,
    button_row,
    quick_embed,
    success_embed,
    error_embed,
    info_embed,
)


class Builders(commands.Cog):
    """Builder showcase cog."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- embed demo -----------------------------------------------------

    @app_commands.command(name="embedtest", description="Demonstrate the EmbedBuilder.")
    async def embedtest(self, interaction: discord.Interaction):
        embed = (
            EmbedBuilder()
            .title("Builder Demo")
            .description("This embed was made with **EmbedBuilder**.")
            .color("green")
            .author(interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
            .field("Field A", "Value A", inline=True)
            .field("Field B", "Value B", inline=True)
            .field("Field C", "A longer value to show the 1024 char limit is respected.")
            .footer("Prowl Builders")
            .timestamp()
            .image("https://picsum.photos/600/200")
            .thumbnail(interaction.guild.icon.url if interaction.guild.icon else "")
            .build()
        )
        await interaction.response.send_message(embed=embed)

    # ---------- quick helpers --------------------------------------------------

    @app_commands.command(name="quicktest", description="Showcase the quick_embed / success / error / info helpers.")
    async def quicktest(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=success_embed("Done!", "Operation completed successfully."))
        # followup for the extra embeds
        await interaction.followup.send(embed=error_embed("Error!", "Something went wrong."))
        await interaction.followup.send(embed=info_embed("Info", "Just so you know."))

    # ---------- button demo ----------------------------------------------------

    @app_commands.command(name="bottest", description="Showcase ButtonBuilder with a click callback.")
    async def bottest(self, interaction: discord.Interaction):

        async def on_yes(interaction: discord.Interaction):
            await interaction.response.send_message("You clicked Yes!", ephemeral=True)

        async def on_no(interaction: discord.Interaction):
            await interaction.response.send_message("You clicked No!", ephemeral=True)

        view = button_row(
            ButtonBuilder().label("Yes").style("success").on_click(on_yes),
            ButtonBuilder().label("No").style("danger").on_click(on_no),
        )
        await interaction.response.send_message("Pick one:", view=view)

    # ---------- link demo ------------------------------------------------------

    @app_commands.command(name="linktest", description="Showcase LinkBuilder as both a button and an embed.")
    async def linktest(self, interaction: discord.Interaction):

        # 1) link button
        link_btn = (
            LinkBuilder()
            .url("https://discord.py.readthedocs.io")
            .label("discord.py Docs")
            .emoji("\U0001f4d6")
        )
        await interaction.response.send_message("Click the link:", view=link_btn.view())

        # 2) link embed
        link_embed = (
            LinkBuilder()
            .url("https://github.com")
            .label("GitHub")
            .description("Where the world builds software.")
            .color("orange")
            .embed()
        )
        await interaction.followup.send(embed=link_embed)

    # ---------- modal demo -----------------------------------------------------

    @app_commands.command(name="modaltest", description="Showcase ModalBuilder (opened via button).")
    async def modaltest(self, interaction: discord.Interaction):

        async def handle_submit(interaction: discord.Interaction, modal: discord.ui.Modal):
            name = modal.children[0].value
            bio = modal.children[1].value
            embed = (
                EmbedBuilder()
                .title("Profile Created")
                .field("Name", name)
                .field("Bio", bio or "No bio provided.")
                .color("blue")
                .build()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        modal = (
            ModalBuilder()
            .title("Create Your Profile")
            .add_input("profile_name", "Display Name", placeholder="What should we call you?")
            .add_input("profile_bio", "Bio", style="paragraph", placeholder="Tell us about yourself...", required=False)
            .on_submit(handle_submit)
        )

        async def open_modal(interaction: discord.Interaction):
            await interaction.response.send_modal(modal.build())

        view = ButtonView(timeout=180)
        btn = ButtonBuilder().label("Open Form").style("primary").on_click(open_modal).build()
        view.add_item(btn)
        await interaction.response.send_message("Click to open the modal:", view=view)


async def setup(bot):
    await bot.add_cog(Builders(bot))
