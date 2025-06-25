import asyncio
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import get
import aiohttp

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
HYPIXEL_KEY = os.environ["HYPIXEL_API_KEY"]
GUILD_ID = 1384308198944669877

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

XP_ROLES = [
    ("Guild Member",         0),
    ("Basic Member",       100),
    ("Upgraded Member",    500),
    ("First Class Member", 1000),
    ("Senior Member",      2500),
    ("Staff Sergeant",     5000),
    ("Technical Sergeant",10000),
    ("Master Sergeant",    25000),
    ("Senior Master Sergeant",50000),
    ("Chief Master Sergeant",100000),
]

BONUS_ROLES = {
    "Basic Member":        ("Junior Enlisted Member",    None),
    "Staff Sergeant":      ("Non-Commission Officer",    "Junior Enlisted Member"),
    "Master Sergeant":     ("Senior Non-Commission Officer", "Non-Commission Officer"),
}

DB_PATH = "xp.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS xp (
    user_id   TEXT PRIMARY KEY,
    xp        INTEGER NOT NULL,
    last_ts   REAL    NOT NULL,
    stars     INTEGER NOT NULL DEFAULT 0,
    ratings   INTEGER NOT NULL DEFAULT 0
)
""")
for col in ("stars", "ratings"):
    try:
        cursor.execute(f"ALTER TABLE xp ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        # column already exists
        pass
conn.commit()


TICKET_REGEX = re.compile(
    r"^(?:kuudra-(?:basic|hot|burning|fiery|infernal)|"
    r"(?:zombie|spider|enderman|wolf|blaze|vampire)-t[1-5]|"
    r"f[1-7]|m[1-7])$",
    re.IGNORECASE
)

giveaway_claims: dict = {}

async def ensure_role(guild: discord.Guild, name: str) -> discord.Role:
    """Get or create a role by name."""
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        role = await guild.create_role(name=name)
    return role

async def apply_xp_roles(member: discord.Member, xp: int):
  guild = member.guild

  current_name = None
  for name, thresh in XP_ROLES:
      if xp >= thresh:
          current_name = name
      else:
          break

  current_role = await ensure_role(guild, current_name)

  xp_roles = [r for r, _ in XP_ROLES]
  to_remove = []
  for r in member.roles:
      if r.name in xp_roles and r.name != current_name:
          # special case: when promoting to Basic Member, keep Guild Member
          if not (current_name == "Basic Member" and r.name == "Guild Member"):
              to_remove.append(r)
  if current_role not in member.roles:
      await member.add_roles(current_role)
  if to_remove:
      await member.remove_roles(*to_remove)

  if current_name in BONUS_ROLES:
      add_name, remove_name = BONUS_ROLES[current_name]
      add_role = await ensure_role(guild, add_name)
      if add_role not in member.roles:
          await member.add_roles(add_role)
      if remove_name:
          rem = discord.utils.get(guild.roles, name=remove_name)
          if rem and rem in member.roles:
              await member.remove_roles(rem)
  for trigger, (bonus, parent) in BONUS_ROLES.items():
      if xp < dict(XP_ROLES)[trigger]:
          bad = discord.utils.get(guild.roles, name=bonus)
          if bad and bad in member.roles:
              await member.remove_roles(bad)



def get_user(user_id: str):
    cursor.execute("SELECT xp, last_ts FROM xp WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        # initialize new user
        now = datetime.now(timezone.utc).timestamp()
        cursor.execute(
            "INSERT INTO xp (user_id, xp, last_ts) VALUES (?, ?, ?)",
            (user_id, 0, now)
        )
        conn.commit()
        return 0, now
    return row[0], row[1]

def update_user(user_id: str, new_xp: int, new_last: float):
    cursor.execute("""
        UPDATE xp
        SET xp = ?, last_ts = ?
        WHERE user_id = ?
    """, (new_xp, new_last, user_id))
    conn.commit()

# --- Helper Functions ---
async def get_or_create_role(guild: discord.Guild, role_name: str) -> discord.Role:
    role = get(guild.roles, name=role_name)
    if not role:
        role = await guild.create_role(name=role_name)
    return role

async def create_ticket_channel(
    user: discord.Member,
    category: discord.CategoryChannel,
    channel_name: str,
    mention_roles: list[str],
    welcome_msg: str
) -> discord.TextChannel:
    guild = category.guild
    channel = await category.create_text_channel(channel_name)
    await channel.set_permissions(guild.default_role, read_messages=False)
    await channel.set_permissions(user, read_messages=True, send_messages=True)

    mentions = []
    for role_name in mention_roles:
        role = await get_or_create_role(guild, role_name)
        await channel.set_permissions(role, read_messages=True, send_messages=True)
        mentions.append(role.mention)

    mention_str = " ".join(mentions)
    await channel.send(f"{mention_str} {user.mention} opened a ticket: **{welcome_msg}**")
    await channel.send(f"Hello {user.mention}! If you no longer need the carry, use `/close`\nPlease do not close the ticket if a carrier has responded to this ticket.\nYou can check a user's rating with `/rating`")
    return channel

# --- UI Components ---
class TierSelect(discord.ui.Select):
    def __init__(self, category_label: str, user: discord.Member, container: discord.TextChannel):
        max_tier = 6 if category_label.lower() in ("zombie", "vampire") else 6
        options = [discord.SelectOption(label=f"T{i}", value=f"t{i}") for i in range(1, max_tier)]
        super().__init__(placeholder="Select a Tier", options=options)
        self.category_label = category_label
        self.user = user
        self.container = container

    async def callback(self, interaction: discord.Interaction):
        tier = self.values[0]
        channel_name = f"{self.category_label.lower()}-{tier}"
        ticket = await create_ticket_channel(
            user=self.user,
            category=self.container.category,
            channel_name=channel_name,
            mention_roles=["Slayer Carrier"],
            welcome_msg=f"{self.category_label} {tier.upper()}"
        )
        await interaction.response.send_message(f"Ticket created: {ticket.mention}", ephemeral=True)

class TicketButton(discord.ui.Button):
    def __init__(self, label: str, style: discord.ButtonStyle, handler: str):
        super().__init__(label=label, style=style)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        if self.handler == "slayer":
            view = discord.ui.View()
            view.add_item(TierSelect(self.label, interaction.user, interaction.channel))
            await interaction.response.send_message("Select a tier:", view=view, ephemeral=True)
        else:
            role_name = f"{self.handler.capitalize()} Carrier"
            channel_label = self.label.lower()
            if self.handler == "kuudra":
                channel_label = f"kuudra-{channel_label}"

            ticket = await create_ticket_channel(
                user=interaction.user,
                category=interaction.channel.category,
                channel_name=channel_label,
                mention_roles=[role_name],
                welcome_msg=self.label
            )
            await interaction.response.send_message(f"Ticket created: {ticket.mention}", ephemeral=True)

class PanelModal(discord.ui.Modal):
    def __init__(self, category: str):
        super().__init__(title=f"{category} Panel Message")
        self.category = category
        self.body = discord.ui.TextInput(label="Message Body", style=discord.TextStyle.paragraph)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Panel created.", ephemeral=True)
        await interaction.channel.send(self.body.value)
        view = discord.ui.View(timeout=None)

        if self.category == "Dungeons":
            for label in [f"F{i}" for i in range(1, 8)] + [f"M{i}" for i in range(1, 8)]:
                style = discord.ButtonStyle.success if label.startswith("F") else discord.ButtonStyle.danger
                view.add_item(TicketButton(label, style, handler="dungeon"))

        elif self.category == "Slayer":
            for label in ["Zombie", "Spider", "Enderman", "Wolf", "Blaze", "Vampire"]:
                view.add_item(TicketButton(label, discord.ButtonStyle.blurple, handler="slayer"))

        elif self.category == "Kuudra":
            for label in ["Basic", "Hot", "Burning", "Fiery", "Infernal"]:
                view.add_item(TicketButton(label, discord.ButtonStyle.danger, handler="kuudra"))

        elif self.category == "Verification":
            button = discord.ui.Button(label="Verify", style=discord.ButtonStyle.success)
            async def verify_callback(inter: discord.Interaction):
                # instead of immediately responding, pop up our modal:
                await inter.response.send_modal(VerifyModal())
            button.callback = verify_callback
            view.add_item(button)

        elif self.category == "Applications":
            button = discord.ui.Button(label="Apply", style=discord.ButtonStyle.success)
            async def apply_callback(inter: discord.Interaction):
                user = inter.user
                category = inter.channel.category
                channel_name = f"application-{user.name.lower()}"
                ticket = await category.create_text_channel(channel_name)
                guild = ticket.guild
                await ticket.set_permissions(guild.default_role, read_messages=False)
                maint_role = await get_or_create_role(guild, "Maintenance")
                await ticket.set_permissions(maint_role, read_messages=True, send_messages=True)
                await ticket.set_permissions(user, read_messages=True, send_messages=True)

                await ticket.send(
                    f"{maint_role.mention} {user.mention} opened an application ticket."
                )
                await ticket.send(
                    f"{user.mention}, please provide a screenshot showing that you meet the requirements."
                )
                await inter.response.send_message(
                    f"Your application ticket has been created: {ticket.mention}", ephemeral=True
                )
            button.callback = apply_callback
            view.add_item(button)

        await interaction.channel.send(view=view)

class ConfirmCloseAll(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=30)
        self.user = user

    @discord.ui.button(label="✅ Confirm Close All Tickets", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("This button isn't for you.", ephemeral=True)
        deleted = 0
        for channel in interaction.guild.text_channels:
            if TICKET_REGEX.match(channel.name):
                try:
                    await channel.delete()
                    deleted += 1
                except:
                    pass
        await interaction.response.edit_message(content=f"✅ Closed {deleted} tickets.", view=None)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("This button isn't for you.", ephemeral=True)
        await interaction.response.edit_message(content="❌ Cancelled.", view=None)
        self.stop()

    @bot.listen("on_message")
    async def on_message_xp(message: discord.Message):
        if message.author.bot or not message.guild:
            return

        uid = str(message.author.id)
        now_ts = datetime.now(timezone.utc).timestamp()

        xp, last_ts = get_user(uid)

        if now_ts - last_ts >= 60:
            xp += 5
            update_user(uid, xp, now_ts)
            await apply_xp_roles(message.author, xp)

        await bot.process_commands(message)

# --- Slash Commands ---
@tree.command(name="finish", description="Finish a ticket and request rating", guild=discord.Object(id=GUILD_ID))
async def finish_command(interaction: discord.Interaction):
    carrier_roles = {"Kuudra Carrier", "Slayer Carrier", "Dungeon Carrier"}
    if not any(r.name in carrier_roles for r in interaction.user.roles):
        return await interaction.response.send_message(
            "❌ You must have a Carrier role to finish this ticket.", ephemeral=True
        )

    if not TICKET_REGEX.match(interaction.channel.name):
        return await interaction.response.send_message(
            "❌ This command can only be used in a ticket channel.", ephemeral=True
        )

    cid = str(interaction.user.id)
    xp, last = get_user(cid)
    xp += 100
    update_user(cid, xp, last)
    await apply_xp_roles(interaction.user, xp)

    # 4) Build a View with 1–5 star buttons
    class RatingView(discord.ui.View):
        def __init__(self, carrier: discord.Member):
            super().__init__(timeout=120)
            self.carrier = carrier
            for stars in range(1, 6):
                self.add_item(self.StarButton(stars))

        class StarButton(discord.ui.Button):
            def __init__(self, stars: int):
                super().__init__(label="⭐" * stars, style=discord.ButtonStyle.primary)
                self.stars = stars

            async def callback(self, interaction: discord.Interaction):
                # 5) Record the rating
                uid = str(self.view.carrier.id)
                cursor.execute("SELECT stars, ratings FROM xp WHERE user_id = ?", (uid,))
                row = cursor.fetchone() or (0, 0)
                total_stars, total_ratings = row
                total_stars += self.stars
                total_ratings += 1
                cursor.execute(
                    "UPDATE xp SET stars = ?, ratings = ? WHERE user_id = ?",
                    (total_stars, total_ratings, uid)
                )
                conn.commit()

                # 6) Confirm and close
                await interaction.response.send_message(
                    f"✅ You rated {self.view.carrier.display_name} {self.stars}⭐!", ephemeral=True
                )
                await interaction.channel.delete()
                self.view.stop()

    # 7) Prompt inside the ticket
    await interaction.response.send_message(
        f"⭐ Please rate your carrier **{interaction.user.display_name}** by clicking below:\n**This will close the ticket.**",
        view=RatingView(interaction.user),
        ephemeral=False
    )


@tree.command(name="rating", description="Check someone's carrier rating", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to check")
async def rating_command(interaction: discord.Interaction, user: discord.User):
    cursor.execute("SELECT stars, ratings FROM xp WHERE user_id = ?", (str(user.id),))
    row = cursor.fetchone()
    if not row or row[1] == 0:
        return await interaction.response.send_message(f"{user.display_name} has no ratings yet.")

    stars, ratings = row
    avg = stars / ratings
    await interaction.response.send_message(f"⭐ {user.display_name} has an average rating of **{avg:.2f}** from {ratings} rating(s).")

@tree.command(name="name", description="Change someone's nickname", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="Member to rename", new_nick="New nickname")
async def name_command(interaction: discord.Interaction, user: discord.Member, new_nick: str):
    maint = await get_or_create_role(interaction.guild, "Maintenance")
    if maint not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("You don't have permission.", ephemeral=True)
    try:
        await user.edit(nick=new_nick)
        await interaction.response.send_message(f"Nickname changed to {new_nick}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Permission denied.", ephemeral=True)

@tree.command(name="closeall", description="Close all ticket channels", guild=discord.Object(id=GUILD_ID))
async def closeall_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    view = ConfirmCloseAll(interaction.user)
    await interaction.response.send_message("Confirm closing all tickets?", view=view, ephemeral=True)

@tree.command(name="panel", description="Create a ticket panel", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(option="Ticket category to generate")
@app_commands.choices(option=[
    app_commands.Choice(name="Verification", value="Verification"),
    app_commands.Choice(name="Dungeons", value="Dungeons"),
    app_commands.Choice(name="Kuudra", value="Kuudra"),
    app_commands.Choice(name="Slayer", value="Slayer"),
    app_commands.Choice(name="Applications", value="Applications"),
])
async def panel_command(interaction: discord.Interaction, option: app_commands.Choice[str]):
    maint = await get_or_create_role(interaction.guild, "Maintenance")
    if maint not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await interaction.response.send_modal(PanelModal(option.value))

@tree.command(name="reroll", description="Reroll giveaway winners", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(message_id="Original giveaway message ID", winners="Number of new winners")
async def reroll_command(interaction: discord.Interaction, message_id: str, winners: int):
    maint = await get_or_create_role(interaction.guild, "Maintenance")
    if maint not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        msg_id = int(message_id)
        message = await interaction.channel.fetch_message(msg_id)
    except Exception:
        return await interaction.response.send_message("Message not found.", ephemeral=True)
    entries = []
    for react in message.reactions:
        if str(react.emoji) == "🎉":
            entries = [u async for u in react.users() if not u.bot]
            break
    if not entries:
        return await interaction.response.send_message("No entries.", ephemeral=True)
    winners_list = random.sample(entries, min(winners, len(entries)))
    mentions = ", ".join(w.mention for w in winners_list)
    reroll_msg = await interaction.channel.send(f"🔁 Rerolled winners: {mentions}! React with 🎁 to claim.", reference=message)
    await reroll_msg.add_reaction("🎁")
    giveaway_claims[reroll_msg.id] = {
        "winners": [w.id for w in winners_list],
        "claimed": set(),
        "prize": "Giveaway Reroll Prize",
        "message_id": reroll_msg.id,
        "channel_id": interaction.channel.id
    }
    await interaction.response.send_message("Reroll complete!", ephemeral=True)

@tree.command(name="close", description="Close this ticket channel", guild=discord.Object(id=GUILD_ID))
async def close_command(interaction: discord.Interaction):
    if not TICKET_REGEX.match(interaction.channel.name):
        return await interaction.response.send_message("Use inside a ticket.", ephemeral=True)
    await interaction.response.send_message("Closing…", ephemeral=True)
    await asyncio.sleep(1)
    await interaction.channel.delete()

@tree.command(name="giveaway", description="Start a giveaway", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(time="Duration (e.g., 10s, 5m)", prize="Prize description", winners="Number of winners")
async def giveaway_command(interaction: discord.Interaction, time: str, prize: str, winners: int):
    maint = await get_or_create_role(interaction.guild, "Maintenance")
    if maint not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("No permission.", ephemeral=True)
    unit = time[-1]
    amount = int(time[:-1])
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = amount * factors.get(unit, 1)
    end_time = discord.utils.utcnow() + timedelta(seconds=seconds)
    embed = discord.Embed(
        title="🎉 Giveaway 🎉",
        description=(f"**Prize:** {prize}\nReact with 🎉 to enter!\nEnds <t:{int(end_time.timestamp())}:R>\nWinners: {winners}"),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Hosted by {interaction.user}")
    msg = await interaction.channel.send(embed=embed)
    await msg.add_reaction("🎉")
    await interaction.response.send_message("Giveaway started!", ephemeral=True)

    await asyncio.sleep(seconds)
    msg = await interaction.channel.fetch_message(msg.id)
    entries = [u async for u in msg.reactions[0].users() if not u.bot]
    if not entries:
        return await interaction.channel.send("No entries found.", reference=msg)
    winners_list = random.sample(entries, min(winners, len(entries)))
    mentions = ", ".join(w.mention for w in winners_list)
    result = await interaction.channel.send(f"🎉 Congrats {mentions}, you won **{prize}**! React with 🎁 to claim.", reference=msg)
    await result.add_reaction("🎁")
    giveaway_claims[result.id] = {
        "winners": [w.id for w in winners_list],
        "claimed": set(),
        "prize": prize,
        "message_id": result.id,
        "channel_id": interaction.channel.id
    }

@tree.command(
    name="xp",
    description="Show your total XP",
    guild=discord.Object(id=GUILD_ID)  # for instant registration in your server
 )
async def xp_command(interaction: discord.Interaction):
     uid = str(interaction.user.id)
     xp_val, _ = get_user(uid)
     await interaction.response.send_message(f"🎖️ You have **{xp_val}** XP!", ephemeral=True)

class VerifyModal(discord.ui.Modal):
  def __init__(self):
      super().__init__(title="Hypixel Guild Verification")
      self.username = discord.ui.TextInput(
          label="Minecraft Username",
          placeholder="Your Minecraft in-game name",
          max_length=16
      )
      self.add_item(self.username)

  async def on_submit(self, interaction: discord.Interaction):
      mc_name = self.username.value.strip()
      await interaction.response.defer(ephemeral=True)

      async with aiohttp.ClientSession() as sess:
          mojang = await sess.get(f"https://api.mojang.com/users/profiles/minecraft/{mc_name}")
          if mojang.status != 200:
              return await interaction.followup.send("❌ Could not find that Minecraft user.")
          uuid = (await mojang.json())["id"]

          guild_resp = await sess.get(
              "https://api.hypixel.net/guild",
              params={"key": HYPIXEL_KEY, "name": "Sky Sanctuary"}
          )
          data = await guild_resp.json()
          if not data.get("success"):
              return await interaction.followup.send("❌ Hypixel API error, try again later.")

          members = data["guild"].get("members", [])
          in_sanctuary = any(m["uuid"] == uuid for m in members)

      role_name = "Guild Member" if in_sanctuary else "Guest"
      opposite  = "Guest" if in_sanctuary else "Guild Member"
      guild      = interaction.guild
      role       = discord.utils.get(guild.roles, name=role_name) or await guild.create_role(name=role_name)
      opp_role   = discord.utils.get(guild.roles, name=opposite)

      await interaction.user.add_roles(role)
      if opp_role and opp_role in interaction.user.roles:
          await interaction.user.remove_roles(opp_role)

      await interaction.followup.send(
          f"✅ {mc_name} {'is' if in_sanctuary else 'is not'} in Sky Sanctuary. "
          f"You’ve been given **{role_name}**.",
          ephemeral=True
      )
@tree.command(
  name="updatexp",
  description="Manually sync Hypixel guild XP and roles",
  guild=discord.Object(id=GUILD_ID)
)
async def updatexp_command(inter: discord.Interaction):  maint = await get_or_create_role(inter.guild, "Maintenance")
  if maint not in inter.user.roles and not inter.user.guild_permissions.administrator:
      return await inter.response.send_message("❌ You don’t have permission to run this.", ephemeral=True)

  await inter.response.defer(ephemeral=True)

  async with aiohttp.ClientSession() as sess:
      resp = await sess.get(
          "https://api.hypixel.net/guild",
          params={"key": HYPIXEL_KEY, "name": "Sky Sanctuary"}
      )
      data = await resp.json()
      if not data.get("success"):
          return await inter.followup.send("❌ Hypixel API error – could not fetch guild.", ephemeral=True)

      today = datetime.utcnow().strftime("%Y-%m-%d")
      roster_xp = {
          m["uuid"]: m.get("expHistory", {}).get(today, 0)
          for m in data["guild"].get("members", [])
      }

  guild_role = discord.utils.get(inter.guild.roles, name="Guild Member")
  guest_role = discord.utils.get(inter.guild.roles, name="Guest") \
               or await inter.guild.create_role(name="Guest")
  if not guild_role:
      return await inter.followup.send("⚠️ No “Guild Member” role found on this server.", ephemeral=True)

  demoted = 0
  xp_awarded = 0

  for member in list(guild_role.members):
      mc_name = member.nick or member.name

      async with aiohttp.ClientSession() as sess:
          mj = await sess.get(f"https://api.mojang.com/users/profiles/minecraft/{mc_name}")
      if mj.status != 200:
          # invalid name → demote
          await member.remove_roles(guild_role)
          await member.add_roles(guest_role)
          demoted += 1
          continue

      uuid = (await mj.json())["id"]
      earned = roster_xp.get(uuid)

      if earned is None:
          await member.remove_roles(guild_role)
          await member.add_roles(guest_role)
          demoted += 1
      else:
          bonus = earned // 1000
          if bonus > 0:
              old_xp, last_ts = get_user(str(member.id))
              new_xp = old_xp + bonus
              update_user(str(member.id), new_xp, last_ts)
              await apply_xp_roles(member, new_xp)
              xp_awarded += bonus

  await inter.followup.send(
      f"✅ Update complete: demoted **{demoted}** users, awarded **{xp_awarded}** XP total.",
      ephemeral=True
  )
@tree.command(
  name="setup",
  description="Create all ticket panels at once",
  guild=discord.Object(id=GUILD_ID)
)
async def setup_command(interaction: discord.Interaction):
  maint = await get_or_create_role(interaction.guild, "Maintenance")
  if maint not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
      return await interaction.response.send_message("No permission.", ephemeral=True)

  await interaction.response.defer(ephemeral=True)

  panels = {
      "slayers":      ("Slayer",       "slayer.txt"),
      "dungeons":     ("Dungeons",     "dungeons.txt"),
      "kuudra":       ("Kuudra",       "kuudra.txt"),
      "verify":       ("Verification", "verify.txt"),
      "applications": ("Applications", "apply.txt"),
  }

  for chan_name, (category, filename) in panels.items():
      channel = get(interaction.guild.text_channels, name=chan_name)
      if not channel:
          continue

      async for msg in channel.history(limit=200):
          if msg.author == bot.user:
              try:
                  await msg.delete()
              except:
                  pass

      try:
          with open(filename, encoding="utf-8") as f:
              body = f.read()
      except FileNotFoundError:
          continue

      view = discord.ui.View(timeout=None)
      if category == "Slayer":
          for label in ["Zombie","Spider","Enderman","Wolf","Blaze","Vampire"]:
              view.add_item(TicketButton(label, discord.ButtonStyle.blurple, handler="slayer"))

      elif category == "Dungeons":
          for label in [f"F{i}" for i in range(1,8)] + [f"M{i}" for i in range(1,8)]:
              style = discord.ButtonStyle.success if label.startswith("F") else discord.ButtonStyle.danger
              view.add_item(TicketButton(label, style, handler="dungeon"))

      elif category == "Kuudra":
          for label in ["Basic","Hot","Burning","Fiery","Infernal"]:
              view.add_item(TicketButton(label, discord.ButtonStyle.danger, handler="kuudra"))

      elif category == "Verification":
          button = discord.ui.Button(label="Verify", style=discord.ButtonStyle.success)
          async def verify_cb(inter: discord.Interaction):
              await inter.response.send_modal(VerifyModal())
          button.callback = verify_cb
          view.add_item(button)

      elif category == "Applications":
          button = discord.ui.Button(label="Apply", style=discord.ButtonStyle.success)
          async def apply_cb(inter: discord.Interaction):
              user = inter.user
              ticket_name = f"application-{user.name.lower()}"
              ticket = await inter.channel.category.create_text_channel(ticket_name)
              guild = ticket.guild
              await ticket.set_permissions(guild.default_role, read_messages=False)
              maint_role = await get_or_create_role(guild, "Maintenance")
              await ticket.set_permissions(maint_role, read_messages=True, send_messages=True)
              await ticket.set_permissions(user, read_messages=True, send_messages=True)
              await ticket.send(f"{maint_role.mention} {user.mention} opened an application ticket.")
              await ticket.send(f"{user.mention}, please provide a screenshot showing you meet the requirements.")
              await inter.response.send_message(
                  f"✅ Your application ticket is {ticket.mention}",
                  ephemeral=True
              )
          button.callback = apply_cb
          view.add_item(button)

      await channel.send(body, view=view)

  await interaction.followup.send("✅ Setup complete.", ephemeral=True)


# --- Event Listeners ---
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id or payload.message_id not in giveaway_claims:
        return
    data = giveaway_claims[payload.message_id]
    if payload.user_id not in data["winners"] or payload.user_id in data["claimed"]:
        return
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    channel = guild.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    category = channel.category
    ticket_name = f"giveaway-{member.name.lower()}"
    # Prevent duplicate tickets
    if any(c.name == ticket_name for c in category.channels):
        return
    ticket = await create_ticket_channel(
        user=member,
        category=category,
        channel_name=ticket_name,
        mention_roles=["Giveaways"],
        welcome_msg=f"Giveaway claim for {data['prize']}"
    )
    data["claimed"].add(payload.user_id)

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    verify_channel = get(guild.text_channels, name="verify")
    if not verify_channel:
        overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False)}
        verify_channel = await guild.create_text_channel("verify", overwrites=overwrites)
    try:
        await member.send(f"Welcome to {guild.name}! Please verify in {verify_channel.mention} to get started.")
    except discord.Forbidden:
        pass
    welcome = get(guild.text_channels, name="welcome")
    if welcome:
        await welcome.send(f"Welcome {member.mention} to the server!")

@tasks.loop(hours=24)
async def daily_guild_check():
    guild_obj = bot.get_guild(GUILD_ID)
    if guild_obj is None:
        return

    async with aiohttp.ClientSession() as sess:
        resp = await sess.get(
            "https://api.hypixel.net/guild",
            params={"key": HYPIXEL_KEY, "name": "Sky Sanctuary"}
        )
        data = await resp.json()
        if not data.get("success"):
            return
        # build a map uuid → XP earned today
        today = datetime.utcnow().strftime("%Y-%m-%d")
        roster_xp = {
            m["uuid"]: m.get("expHistory", {}).get(today, 0)
            for m in data["guild"].get("members", [])
        }

    guild_role = discord.utils.get(guild_obj.roles, name="Guild Member")
    guest_role = discord.utils.get(guild_obj.roles, name="Guest") or \
                 await guild_obj.create_role(name="Guest")
    if not guild_role:
        return

    for member in list(guild_role.members):
        # use their nickname if set, otherwise their username
        mc_name = member.nick or member.name

        async with aiohttp.ClientSession() as sess:
            mj = await sess.get(f"https://api.mojang.com/users/profiles/minecraft/{mc_name}")
            if mj.status != 200:
                # bad name → demote immediately
                await member.remove_roles(guild_role)
                await member.add_roles(guest_role)
                continue
            uuid = (await mj.json())["id"]

        earned = roster_xp.get(uuid)
        if earned is None:
            # not found → left the guild
            await member.remove_roles(guild_role)
            await member.add_roles(guest_role)
        else:
            # still in guild → award floor(earned/1000) XP
            bonus = earned // 1000
            if bonus > 0:
                old_xp, last_ts = get_user(str(member.id))
                new_xp = old_xp + bonus
                update_user(str(member.id), new_xp, last_ts)
                await apply_xp_roles(member, new_xp)

@daily_guild_check.before_loop
async def wait_ready():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    if not daily_guild_check.is_running():
      daily_guild_check.start()
    print(f"Bot ready as {bot.user}")

# --- Run Bot ---
bot.run(TOKEN)
