# main.py (final fokus DRM + Not Found)
import os
import json
import time
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build
import aiohttp
import requests
from bs4 import BeautifulSoup

# =============== KEEP-ALIVE ===============
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# =============== DISCORD SETUP ===============
intents = discord.Intents.default()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")

# =============== GOOGLE DRIVE SETUP ===============
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")
if not SERVICE_ACCOUNT_INFO:
    raise ValueError("GDRIVE_CREDENTIALS environment variable is required")

creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# =============== CONFIG PERSISTENCE ===============
CONFIG_FILE = "bot_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

def ensure_guild_config(guild_id: int):
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {
            "upload_channel": None,
            "update_channel": None,
            "request_channel": None,
            "request_role": None
        }
        save_config(config)

# =============== STEAM INFO & DRM HELPER ===============
async def fetch_steam_info(appid: str):
    info = {
        "name": f"AppID {appid}",
        "header": None,
        "steam": f"https://store.steampowered.com/app/{appid}",
        "steamdb": f"https://steamdb.info/app/{appid}",
        "release_date": "Unknown",
        "description": "",
        "drm": None
    }
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
                entry = data.get(str(appid), {})
                if entry.get("success"):
                    g = entry["data"]
                    info["name"] = g.get("name", f"AppID {appid}")
                    info["header"] = g.get("header_image")
                    info["release_date"] = g.get("release_date", {}).get("date", "Unknown")
                    info["description"] = g.get("short_description", "")[:500]
    except Exception as e:
        print("Steam API error:", e)

    # Scrape DRM dari SteamDB
    try:
        url = f"https://steamdb.info/app/{appid}/info/"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            drm_row = soup.find("td", string="3rd-Party DRM")
            if drm_row and drm_row.find_next("td"):
                info["drm"] = drm_row.find_next("td").get_text(strip=True)
    except Exception as e:
        print("SteamDB DRM scrape error:", e)

    return info

# =============== /gen COMMAND ===============
@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    ensure_guild_config(interaction.guild_id)
    await interaction.response.defer(ephemeral=False)

    start_t = time.perf_counter()
    try:
        q = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(
            q=q, fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute()
        items = results.get("files", [])

        info = await fetch_steam_info(appid)
        elapsed = time.perf_counter() - start_t

        # Not Found
        if not items:
            embed_nf = discord.Embed(
                title="❌ Game Requested (Not Found)",
                description=f"User {interaction.user.mention} request AppID **{appid}**",
                color=discord.Color.red()
            )
            embed_nf.add_field(name="Steam Store", value=f"[Open]({info['steam']})", inline=True)
            embed_nf.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
            if info["header"]:
                embed_nf.set_image(url=info["header"])
            embed_nf.set_footer(text="Requested via /gen")

            await interaction.followup.send(embed=embed_nf)

            conf = config.get(str(interaction.guild_id), {})
            ch_id = conf.get("request_channel")
            role_id = conf.get("request_role")
            if ch_id:
                ch = bot.get_channel(ch_id)
                if ch:
                    mention_txt = f"<@&{role_id}>" if role_id else ""
                    await ch.send(content=mention_txt, embed=embed_nf)
            return

        # Found
        f = items[0]
        file_id = f["id"]
        file_name = f["name"]
        size_kb = int(f.get("size", 0)) // 1024

        embed = discord.Embed(
            title="✅ Manifest Retrieved",
            color=discord.Color.gold() if info["drm"] else discord.Color.purple()
        )
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)
        embed.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
        embed.add_field(name="⏱️ Time", value=f"{elapsed:.2f}s", inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        embed.add_field(name="📥 Download", value="File hanya bisa diunduh oleh requester (lihat bawah).", inline=False)
        if info["description"]:
            embed.add_field(name="ℹ️ Info", value=info["description"], inline=False)
        if info["header"]:
            embed.set_image(url=info["header"])

        if info["drm"]:
            embed.add_field(name="⚠️ Important Notes", value=f"NOTICE: {info['drm']}", inline=False)

        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed)

        # file untuk requester
        tmp_path = f"/tmp/{file_name}"
        try:
            r = requests.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                headers={"Authorization": f"Bearer {creds.token}"},
                stream=True, timeout=120
            )
            with open(tmp_path, "wb") as out_f:
                for chunk in r.iter_content(chunk_size=1 << 14):
                    if chunk:
                        out_f.write(chunk)
            await interaction.followup.send(
                content="📥 File manifest siap diunduh :",
                file=discord.File(tmp_path, file_name),
                ephemeral=True
            )
        except Exception as ex:
            await interaction.followup.send(f"⚠️ Gagal unduh file: {ex}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error saat /gen: {e}", ephemeral=False)

# =============== /channelrequestsetup ===============
@tree.command(name="channelrequestsetup", description="Set channel untuk notif request not found + role mention")
async def channelrequestsetup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["request_channel"] = channel.id
    config[str(interaction.guild_id)]["request_role"] = role.id if role else None
    save_config(config)
    await interaction.response.send_message(
        f"✅ Channel Request diset ke {channel.mention}, role: {role.mention if role else 'None'}"
    )

# =============== ON READY ===============
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot logged in as {bot.user} — in {len(bot.guilds)} guilds")

# =============== START ===============
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
