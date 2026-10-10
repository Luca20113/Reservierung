
import os
import sqlite3
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

import discord
from discord.ext import commands
from dotenv import load_dotenv


# ==================================================
# KONFIGURATION
# ==================================================

load_dotenv()

TOKEN = os.getenv("TOKEN", "").strip()
PREFIX = "!"

DB_PATH = os.getenv("DB_PATH", "reservations.db")

RESERVATION_CHANNEL_ID = int(
    os.getenv("RESERVATION_CHANNEL_ID", "0")
)

COMMAND_CHANNEL_ID = int(
    os.getenv("COMMAND_CHANNEL_ID", "0")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

if not TOKEN:
    raise RuntimeError(
        "TOKEN fehlt! Bitte in der .env-Datei konfigurieren."
    )

if RESERVATION_CHANNEL_ID == 0:
    raise RuntimeError(
        "RESERVATION_CHANNEL_ID fehlt! "
        "Bitte die Zielkanal-ID in der .env-Datei eintragen."
    )


# ==================================================
# DISCORD BOT
# ==================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents
)


# ==================================================
# KATEGORIEN
# ==================================================

CATEGORIES = [
    "Flag",
    "Doppelhalter",
    "Pyro",
    "Drummer",
]

CATEGORY_EMOJI = {
    "Flag": "🚩",
    "Doppelhalter": "🏳️",
    "Pyro": "🎇",
    "Drummer": "🥁",
}


# ==================================================
# DATENBANK
# ==================================================

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
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


def get_reserved_names(
    message_id: int,
    category: str,
    guild: discord.Guild
) -> list[str]:

    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id
            FROM reservations
            WHERE message_id = ? AND category = ?
            ORDER BY created_at
            """,
            (message_id, category)
        ).fetchall()

    names = []

    for (user_id,) in rows:
        member = guild.get_member(user_id)

        if member is not None:
            names.append(member.display_name)
        else:
            names.append(f"User {user_id}")

    return names


# ==================================================
# EMBED ERSTELLEN
# ==================================================

def build_embed(
    message_id: int,
    game: str,
    when_text: str,
    categories: list[str],
    guild: discord.Guild
) -> discord.Embed:

    embed = discord.Embed(
        title=f"📋 Reservierung: {game}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="🎮 Spiel",
        value=game,
        inline=True
    )

    embed.add_field(
        name="🕒 Wann?",
        value=when_text,
        inline=True
    )

    for category in categories:
        names = get_reserved_names(
            message_id,
            category,
            guild
        )

        if names:
            value = "\n".join(
                f"• {name}" for name in names
            )
        else:
            value = "Noch niemand reserviert"

        # Discord erlaubt maximal 1024 Zeichen pro Feld.
        if len(value) > 1024:
            value = value[:1010] + "\n… weitere Einträge"

        embed.add_field(
            name=(
                f"{CATEGORY_EMOJI.get(category, '📌')} "
                f"{category} ({len(names)})"
            ),
            value=value,
            inline=False
        )

    embed.set_footer(
        text=(
            "Klicke auf eine Schaltfläche, um dich "
            "einzutragen oder wieder auszutragen."
        )
    )

    return embed


# ==================================================
# RESERVIERUNGS-BUTTONS
# ==================================================

class ReservationButton(discord.ui.Button):

    def __init__(self, category: str):
        super().__init__(
            label=f"{category} reservieren",
            emoji=CATEGORY_EMOJI.get(category),
            style=discord.ButtonStyle.primary,
            custom_id=f"reserve:{category}"
        )

        self.category = category

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if (
            interaction.guild is None
            or interaction.message is None
        ):
            await interaction.response.send_message(
                "Das geht nur auf einem Server.",
                ephemeral=True
            )
            return

        message_id = interaction.message.id
        user_id = interaction.user.id

        with db() as conn:
            existing = conn.execute(
                """
                SELECT 1
                FROM reservations
                WHERE message_id = ?
                  AND category = ?
                  AND user_id = ?
                """,
                (message_id, self.category, user_id)
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    DELETE FROM reservations
                    WHERE message_id = ?
                      AND category = ?
                      AND user_id = ?
                    """,
                    (message_id, self.category, user_id)
                )

                status = (
                    f"Du bist bei **{self.category}** "
                    "nicht mehr eingetragen."
                )

            else:
                conn.execute(
                    """
                    INSERT INTO reservations
                    (message_id, category, user_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        self.category,
                        user_id,
                        datetime.now(timezone.utc).isoformat()
                    )
                )

                status = (
                    f"Du bist für **{self.category}** "
                    "eingetragen."
                )

            row = conn.execute(
                """
                SELECT game, when_text, categories
                FROM reservation_posts
                WHERE message_id = ?
                """,
                (message_id,)
            ).fetchone()

        if row:
            game, when_text, categories_csv = row
            categories = categories_csv.split(",")

            await interaction.response.edit_message(
                embed=build_embed(
                    message_id,
                    game,
                    when_text,
                    categories,
                    interaction.guild
                ),
                view=ReservationView(categories)
            )

            await interaction.followup.send(
                status,
                ephemeral=True
            )

        else:
            await interaction.response.send_message(
                "Diese Reservierung wurde nicht in der "
                "Datenbank gefunden. Bitte erstelle sie neu.",
                ephemeral=True
            )


class ReservationView(discord.ui.View):

    def __init__(self, categories: list[str]):
        super().__init__(timeout=None)

        for category in categories:
            self.add_item(ReservationButton(category))


# ==================================================
# KATEGORIE-AUSWAHL
# ==================================================

class CategorySelect(discord.ui.Select):

    def __init__(self):
        options = [
            discord.SelectOption(
                label=category,
                emoji=CATEGORY_EMOJI.get(category),
                value=category
            )
            for category in CATEGORIES
        ]

        super().__init__(
            placeholder="Welche Kategorien möchtest du reservieren?",
            min_values=1,
            max_values=len(CATEGORIES),
            options=options,
            custom_id="reservation:categories"
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        if interaction.guild is None:
            await interaction.response.send_message(
                "Das geht nur auf einem Server.",
                ephemeral=True
            )
            return

        if not isinstance(self.view, CategorySelectView):
            await interaction.response.send_message(
                "Das Formular ist abgelaufen. "
                "Bitte starte es erneut.",
                ephemeral=True
            )
            return

        game = self.view.game
        when_text = self.view.when_text
        categories = list(self.values)

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        try:
            channel = bot.get_channel(
                RESERVATION_CHANNEL_ID
            )

            if channel is None:
                channel = await bot.fetch_channel(
                    RESERVATION_CHANNEL_ID
                )

            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send(
                    "Der Zielkanal ist kein Textkanal. "
                    "Bitte prüfe RESERVATION_CHANNEL_ID.",
                    ephemeral=True
                )
                return

            if channel.guild.id != interaction.guild.id:
                await interaction.followup.send(
                    "Der Zielkanal muss auf demselben "
                    "Server liegen.",
                    ephemeral=True
                )
                return

            # Nachricht erstellen, damit wir ihre ID erhalten.
            message = await channel.send(
                "Reservierung wird erstellt …"
            )

            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO reservation_posts
                    (
                        message_id,
                        guild_id,
                        channel_id,
                        game,
                        when_text,
                        categories
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        interaction.guild.id,
                        channel.id,
                        game,
                        when_text,
                        ",".join(categories)
                    )
                )

            await message.edit(
                content=None,
                embed=build_embed(
                    message.id,
                    game,
                    when_text,
                    categories,
                    channel.guild
                ),
                view=ReservationView(categories)
            )

            await interaction.followup.send(
                f"Fertig! Deine Reservierung wurde in "
                f"{channel.mention} veröffentlicht.",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "Der Bot hat keine Berechtigung, "
                "im Zielkanal Nachrichten zu senden oder "
                "zu bearbeiten. Prüfe seine Rechte.",
                ephemeral=True
            )

        except discord.HTTPException as error:
            logging.exception(
                "Discord-Fehler beim Erstellen der Reservierung"
            )

            await interaction.followup.send(
                f"Die Reservierung konnte nicht erstellt "
                f"werden. Discord-Fehler: {error}",
                ephemeral=True
            )

        except Exception:
            logging.exception(
                "Fehler beim Erstellen der Reservierung"
            )

            await interaction.followup.send(
                "Ein unerwarteter Fehler ist aufgetreten. "
                "Bitte prüfe die Server-Logs.",
                ephemeral=True
            )


class CategorySelectView(discord.ui.View):

    def __init__(self, game: str, when_text: str):
        super().__init__(timeout=300)

        self.game = game
        self.when_text = when_text

        self.add_item(CategorySelect())


# ==================================================
# FORMULAR
# ==================================================

class ReservationModal(
    discord.ui.Modal,
    title="Neue Reservierung"
):

    spiel = discord.ui.TextInput(
        label="Spiel?",
        placeholder="z. B. FC Beispiel gegen SV Muster",
        max_length=100
    )

    wann = discord.ui.TextInput(
        label="Wann?",
        placeholder="z. B. Samstag, 18:30 Uhr",
        max_length=100
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        game = str(self.spiel.value).strip()
        when_text = str(self.wann.value).strip()

        if not game or not when_text:
            await interaction.response.send_message(
                "Bitte fülle beide Felder aus.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Wähle jetzt eine oder mehrere Kategorien aus:",
            view=CategorySelectView(game, when_text),
            ephemeral=True
        )


class OpenModalView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(
        label="Reservierung erstellen",
        style=discord.ButtonStyle.success,
        emoji="📝"
    )
    async def open_modal(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            ReservationModal()
        )


# ==================================================
# BOT START
# ==================================================

views_registered = False


@bot.event
async def on_ready():
    global views_registered

    if not views_registered:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT message_id, categories
                FROM reservation_posts
                """
            ).fetchall()

        for message_id, categories_csv in rows:
            categories = categories_csv.split(",")

            bot.add_view(
                ReservationView(categories),
                message_id=message_id
            )

        views_registered = True

    logging.info(
        "Eingeloggt als %s (ID: %s)",
        bot.user,
        bot.user.id if bot.user else "unbekannt"
    )


# ==================================================
# BEFEHL: !reservierung
# ==================================================

@bot.command(name="reservierung")
@commands.guild_only()
async def reservierung(ctx: commands.Context):

    if (
        COMMAND_CHANNEL_ID != 0
        and ctx.channel.id != COMMAND_CHANNEL_ID
    ):
        await ctx.reply(
            f"Dieser Befehl ist nur in "
            f"<#{COMMAND_CHANNEL_ID}> erlaubt.",
            delete_after=10,
            mention_author=False
        )
        return

    await ctx.reply(
        "Klicke auf **Reservierung erstellen**, "
        "um das Formular zu öffnen.",
        view=OpenModalView(),
        mention_author=False
    )


# ==================================================
# START
# ==================================================

if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)
