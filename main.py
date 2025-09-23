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

# =============== KEEP-ALIVE (OPTIONAL) ===============
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

# =============== CONFIG FILE ===============
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
            "request_role": None,
            "patch_channel": None
        }
        save_config(config)

# =============== DRIVE CACHE ===============
known_files = {}
known_patches = {}
ENABLE_UPLOAD_WATCH = False

def initialize_known_files():
    global known_files
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id, name, modifiedTime, size)"
        ).execute()
        items = results.get("files", [])
        known_files = {
            f["name"]: {"id": f["id"], "mtime": f.get("modifiedTime", ""), "size": f.get("size", "0")}
            for f in items
        }
        print(f"Initialized cache: {len(known_files)} files.")
    except Exception as e:
        print("Error initializing known_files:", e)

# =============== STEAM + DRM INFO ===============
async def fetch_steam_info(appid: str):
    base_info = {
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
            # Steam API
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
                entry = data.get(str(appid), {})
                if entry.get("success"):
                    g = entry["data"]
                    base_info["name"] = g.get("name", base_info["name"])
                    base_info["header"] = g.get("header_image")
                    base_info["description"] = g.get("short_description", "")[:400]
                    if g.get("release_date", {}).get("date"):
                        base_info["release_date"] = g["release_date"]["date"]

            # SteamDB scraping for DRM
            drm_url = f"https://steamdb.info/app/{appid}/info/"
            async with session.get(drm_url, timeout=15) as resp:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                rows = soup.select("table tr")
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) >= 2 and "3rd-Party DRM" in cols[0].get_text():
                        base_info["drm"] = cols[1].get_text(strip=True)
                        break
    except Exception as e:
        print("fetch_steam_info error:", e)
    return base_info

# =============== /gen COMMAND ===============
@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    if interaction.guild_id:
        ensure_guild_config(interaction.guild_id)

    await interaction.response.defer(ephemeral=False)
    start_t = time.perf_counter()

    try:
        q = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(
            q=q, fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute()
        items = results.get("files", [])

        if not items:
            await interaction.followup.send(f"❌ File untuk AppID `{appid}` tidak ditemukan.", ephemeral=False)
            if interaction.guild_id:
                conf = config.get(str(interaction.guild_id), {})
                req_ch = conf.get("request_channel")
                mention_role = conf.get("request_role")
                if req_ch:
                    ch = bot.get_channel(req_ch)
                    if ch:
                        info = await fetch_steam_info(appid)
                        embed_nf = discord.Embed(
                            title="📌 Game Requested (Not Found)",
                            description=f"User {interaction.user.mention} request AppID **{appid}**",
                            color=discord.Color.red()
                        )
                        embed_nf.add_field(name="Steam Store", value=f"[Open]({info['steam']})", inline=True)
                        embed_nf.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
                        embed_nf.set_footer(text="Requested via /gen")
                        mention_text = f"<@&{mention_role}>" if mention_role else ""
                        await ch.send(content=mention_text, embed=embed_nf)
            return

        f = items[0]
        file_id, file_name = f["id"], f["name"]
        size_bytes = int(f.get("size", 0))
        size_kb = size_bytes // 1024

        info = await fetch_steam_info(appid)
        elapsed = time.perf_counter() - start_t

        color_embed = discord.Color.red() if info["drm"] else discord.Color.purple()
        embed = discord.Embed(title="✅ Manifest Retrieved", color=color_embed)
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)
        embed.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
        embed.add_field(name="⏱️ Time", value=f"{elapsed:.2f}s", inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        embed.add_field(name="📥 Download", value="File hanya bisa diunduh oleh requester (lihat bawah).", inline=False)
        if info["drm"]:
            embed.add_field(name="🔒 DRM", value=info["drm"], inline=False)
        if info["description"]:
            embed.add_field(name="ℹ️ Info", value=info["description"], inline=False)
        if info.get("header"):
            embed.set_image(url=info["header"])
        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed, ephemeral=False)

        tmp_path = f"/tmp/{file_name}"
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

    except Exception as e:
        await interaction.followup.send(f"❌ Error saat menjalankan /gen: {e}", ephemeral=False)

# =============== PATCH CHECK LOOP ===============
@tasks.loop(hours=1)
async def check_patches():
    try:
        for fname in list(known_files.keys()):
            appid = fname.replace(".zip", "")
            info = await fetch_steam_info(appid)
            # SteamDB patch check
            patch_url = f"https://steamdb.info/api/Patchnotes/{appid}/"
            async with aiohttp.ClientSession() as session:
                async with session.get(patch_url, timeout=15) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not data.get("data"):
                        continue
                    latest_patch = data["data"][0]
                    patch_id = str(latest_patch.get("id"))
                    if known_patches.get(appid) != patch_id:
                        known_patches[appid] = patch_id
                        # broadcast patch update
                        for gid, conf in config.items():
                            ch_id = conf.get("patch_channel")
                            if ch_id:
                                ch = bot.get_channel(ch_id)
                                if ch:
                                    embed = discord.Embed(
                                        title=f"🟠 Patch Update: {info['name']}",
                                        description=latest_patch.get("title",""),
                                        color=discord.Color.orange()
                                    )
                                    embed.add_field(name="AppID", value=appid, inline=True)
                                    embed.add_field(name="Date", value=latest_patch.get("time",""), inline=True)
                                    embed.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=False)
                                    if info.get("header"):
                                        embed.set_image(url=info["header"])
                                    await ch.send(embed=embed)
    except Exception as e:
        print("check_patches error:", e)

# =============== COMMAND SETUP CHANNELS ===============
@tree.command(name="channeluploadsetup", description="Set channel untuk notif file baru (Added)")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Added** diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="Set channel untuk notif file update (Updated)")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Updated** diset ke {channel.mention}")

@tree.command(name="channelrequestsetup", description="Set channel untuk notif Request Not Found + role mention")
async def channelrequestsetup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["request_channel"] = channel.id
    config[str(interaction.guild_id)]["request_role"] = role.id if role else None
    save_config(config)
    mention_txt = f" + role {role.mention}" if role else ""
    await interaction.response.send_message(f"✅ Channel **Request Not Found** diset ke {channel.mention}{mention_txt}")

@tree.command(name="channelpatchsetup", description="Set channel untuk notif Patch Update")
async def channelpatchsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["patch_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Patch Update** diset ke {channel.mention}")

# =============== BOT READY ===============
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot logged in as {bot.user} — in {len(bot.guilds)} guilds")
    initialize_known_files()
    check_patches.start()

# =============== START BOT ===============
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
