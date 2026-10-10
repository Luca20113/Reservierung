import os
import sqlite3
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0"))
RESERVATION_CHANNEL_ID = int(os.getenv("RESERVATION_CHANNEL_ID", "0"))
PREFIX = os.getenv("COMMAND_PREFIX", "!")
DB_PATH = os.getenv("DATABASE_PATH", "reservierungen.sqlite3")

CATEGORIES = ["Flag", "Doppelhalter", "Pyro", "Drummer"]
CATEGORY_EMOJI = {
    "Flag": "🚩",
    "Doppelhalter": "🏳️",
    "Pyro": "🎇",
    "Drummer": "🥁",
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reservation_posts (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            game TEXT NOT NULL,
            when_text TEXT NOT NULL,
            categories TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            message_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (message_id, category, user_id)
        )
    """)
    conn.commit()
    return conn


def get_reserved_names(message_id: int, category: str, guild: discord.Guild) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM reservations WHERE message_id=? AND category=? ORDER BY created_at",
            (message_id, category),
        ).fetchall()
    names = []
    for (user_id,) in rows:
        member = guild.get_member(user_id)
        names.append(member.display_name if member else f"User {user_id}")
    return names


def build_embed(message_id: int, game: str, when_text: str, categories: list[str],
                guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 Reservierung: {game}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🎮 Spiel", value=game, inline=True)
    embed.add_field(name="🕒 Wann?", value=when_text, inline=True)
    for category in categories:
        names = get_reserved_names(message_id, category, guild)
        value = "\n".join(f"• {name}" for name in names) if names else "Noch niemand reserviert"
        embed.add_field(
            name=f"{CATEGORY_EMOJI.get(category, '📌')} {category} ({len(names)})",
            value=value[:1024],
            inline=False,
        )
    embed.set_footer(text="Klicke auf eine Schaltfläche, um dich einzutragen oder wieder auszutragen.")
    return embed


class ReservationButton(discord.ui.Button):
    def __init__(self, category: str):
        super().__init__(
            label=f"{category} reservieren",
            emoji=CATEGORY_EMOJI.get(category),
            style=discord.ButtonStyle.primary,
            custom_id=f"reserve:{category}",
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.message is None:
            await interaction.response.send_message("Das geht nur auf dem Server.", ephemeral=True)
            return

        message_id = interaction.message.id
        user_id = interaction.user.id
        with db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM reservations WHERE message_id=? AND category=? AND user_id=?",
                (message_id, self.category, user_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM reservations WHERE message_id=? AND category=? AND user_id=?",
                    (message_id, self.category, user_id),
                )
                status = f"Du bist bei **{self.category}** nicht mehr eingetragen."
            else:
                conn.execute(
                    "INSERT INTO reservations(message_id, category, user_id, created_at) VALUES(?,?,?,?)",
                    (message_id, self.category, user_id, datetime.now(timezone.utc).isoformat()),
                )
                status = f"Du bist für **{self.category}** eingetragen."

            row = conn.execute(
                "SELECT game, when_text, categories FROM reservation_posts WHERE message_id=?",
                (message_id,),
            ).fetchone()

        if row:
            game, when_text, cats_csv = row
            cats = cats_csv.split(",")
            await interaction.response.edit_message(
                embed=build_embed(message_id, game, when_text, cats, interaction.guild),
                view=ReservationView(cats),
            )
            await interaction.followup.send(status, ephemeral=True)
        else:
            await interaction.response.send_message(status, ephemeral=True)


class ReservationView(discord.ui.View):
    def __init__(self, categories: list[str]):
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(ReservationButton(category))


class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=cat, emoji=CATEGORY_EMOJI.get(cat), value=cat)
            for cat in CATEGORIES
        ]
        super().__init__(
            placeholder="Was möchtest du reservieren? (Mehrfachauswahl möglich)",
            min_values=1,
            max_values=len(CATEGORIES),
            options=options,
            custom_id="reservation:categories",
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(self.view, CategorySelectView):
            await interaction.response.send_message("Formular abgelaufen. Bitte starte es erneut.", ephemeral=True)
            return
        game = self.view.game
        when_text = self.view.when_text
        categories = list(self.values)

        channel = bot.get_channel(RESERVATION_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Der Zielkanal wurde nicht gefunden. Prüfe RESERVATION_CHANNEL_ID in der .env-Datei.",
                ephemeral=True,
            )
            return

        # Erst Platzhalter senden, damit die Message-ID für die Datenbank verfügbar ist.
        message = await channel.send("Reservierung wird erstellt …")
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO reservation_posts(message_id, guild_id, channel_id, game, when_text, categories) "
                "VALUES(?,?,?,?,?,?)",
                (message.id, interaction.guild_id or 0, channel.id, game, when_text, ",".join(categories)),
            )
        await message.edit(
            content=None,
            embed=build_embed(message.id, game, when_text, categories, channel.guild),
            view=ReservationView(categories),
        )
        await interaction.response.edit_message(
            content=f"Fertig! Deine Reservierung wurde in {channel.mention} veröffentlicht.",
            view=None,
        )


class CategorySelectView(discord.ui.View):
    def __init__(self, game: str, when_text: str):
        super().__init__(timeout=300)
        self.game = game
        self.when_text = when_text
        self.add_item(CategorySelect())


class ReservationModal(discord.ui.Modal, title="Neue Reservierung"):
    spiel = discord.ui.TextInput(
        label="Spiel?",
        placeholder="z. B. FC Beispiel gegen SV Muster",
        max_length=100,
    )
    wann = discord.ui.TextInput(
        label="Wann?",
        placeholder="z. B. Samstag, 18:30 Uhr",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Wähle jetzt eine oder mehrere Kategorien aus:",
            view=CategorySelectView(str(self.spiel.value).strip(), str(self.wann.value).strip()),
            ephemeral=True,
        )


class OpenModalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Reservierung erstellen", style=discord.ButtonStyle.success, emoji="📝")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReservationModal())


@bot.event
async def on_ready():
    db().close()
    # Registriert die Buttons bestehender Reservierungsnachrichten nach einem Neustart erneut.
    with db() as conn:
        rows = conn.execute(
            "SELECT message_id, categories FROM reservation_posts"
        ).fetchall()
    for message_id, cats_csv in rows:
        bot.add_view(ReservationView(cats_csv.split(",")), message_id=message_id)
    logging.info("Eingeloggt als %s", bot.user)


@bot.command(name="reservierung")
@commands.guild_only()
async def reservierung(ctx: commands.Context):
    if COMMAND_CHANNEL_ID and ctx.channel.id != COMMAND_CHANNEL_ID:
        await ctx.reply(
            f"Dieser Befehl ist nur in <#{COMMAND_CHANNEL_ID}> erlaubt.",
            delete_after=10,
            mention_author=False,
        )
        return
    # Nur den Aufrufer kurz informieren; die Formular-Schaltfläche ist im Kanal sichtbar.
    await ctx.reply(
        "Klicke auf **Reservierung erstellen**, um das Formular zu öffnen.",
        view=OpenModalView(),
        mention_author=False,
    )


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN fehlt. Lege eine .env-Datei anhand von .env.example an.")

if RESERVATION_CHANNEL_ID == 0:
    raise RuntimeError("RESERVATION_CHANNEL_ID fehlt oder ist 0. Trage die Zielkanal-ID in .env ein.")

bot.run(TOKEN)
