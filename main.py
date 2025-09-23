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

# ================= KEEP-ALIVE =================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# ================= DISCORD SETUP =================
intents = discord.Intents.default()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")

# ================= GOOGLE DRIVE =================
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")
if not SERVICE_ACCOUNT_INFO:
    raise ValueError("GDRIVE_CREDENTIALS env var required")

creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# ================= CONFIG (per guild) =================
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
# per guild:
# { guild_id: {"upload_channel":id,"update_channel":id,"request_channel":id,"request_role":id,"patch_channel":id}}

def ensure_guild_config(gid: int):
    g = str(gid)
    if g not in config:
        config[g] = {
            "upload_channel": None,
            "update_channel": None,
            "request_channel": None,
            "request_role": None,
            "patch_channel": None
        }
        save_config(config)

# ================= DRIVE CACHE =================
known_files = {}
known_patches = {}  # {appid: last_patch_id}
ENABLE_UPLOAD_WATCH = False

def initialize_known_files():
    global known_files
    try:
        items = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id,name,modifiedTime,size,createdTime)"
        ).execute().get("files", [])
        known_files = {
            f["name"]: {
                "id": f["id"],
                "mtime": f.get("modifiedTime", ""),
                "ctime": f.get("createdTime", ""),
                "size": f.get("size", "0")
            }
            for f in items
        }
        print(f"Initialized cache: {len(known_files)} files")
    except Exception as e:
        print("Init cache error:", e)

# ================= STEAM HELPERS =================
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

        # DRM check (SteamDB)
        drm_url = f"https://steamdb.info/app/{appid}/info/"
        async with aiohttp.ClientSession() as session:
            async with session.get(drm_url, timeout=15) as resp:
                html = await resp.text()
                if "3rd-Party DRM" in html:
                    start = html.find("3rd-Party DRM")
                    snippet = html[start:start+200]
                    base_info["drm"] = snippet.split("</td>")[-1].strip().split("<")[0]
    except Exception:
        pass
    return base_info

async def fetch_latest_patch(appid: str):
    """Ambil patch terbaru dari SteamDB API"""
    try:
        url = f"https://steamdb.info/api/patches/{appid}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                patches = data.get("data", [])
                if not patches:
                    return None
                return patches[0]  # patch terbaru
    except Exception:
        return None

# ================= /gen =================
@tree.command(name="gen", description="Ambil manifest dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    ensure_guild_config(interaction.guild_id)
    await interaction.response.defer(ephemeral=False)

    try:
        q = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        items = drive_service.files().list(
            q=q, fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute().get("files", [])

        if not items:
            info = await fetch_steam_info(appid)
            conf = config.get(str(interaction.guild_id), {})
            req_ch, req_role = conf.get("request_channel"), conf.get("request_role")
            if req_ch:
                ch = bot.get_channel(req_ch)
                if ch:
                    embed_nf = discord.Embed(
                        title="❌ Game Requested (Not Found)",
                        description=f"{interaction.user.mention} request AppID **{appid}**",
                        color=discord.Color.red()
                    )
                    embed_nf.add_field(name="Steam Store", value=f"[Open]({info['steam']})")
                    embed_nf.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})")
                    if info.get("header"): embed_nf.set_image(url=info["header"])
                    if req_role:
                        role = interaction.guild.get_role(req_role)
                        await ch.send(content=role.mention if role else None, embed=embed_nf)
                    else:
                        await ch.send(embed=embed_nf)
            await interaction.followup.send("❌ File tidak ditemukan.")
            return

        f = items[0]
        file_id, file_name = f["id"], f["name"]
        size_kb = int(f.get("size", 0)) // 1024
        info = await fetch_steam_info(appid)

        color = discord.Color.red() if info.get("drm") else discord.Color.purple()
        embed = discord.Embed(title="✅ Manifest Retrieved", color=color)
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)
        embed.add_field(name="📅 Last Update", value=info["release_date"], inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)
        if info.get("drm"):
            embed.add_field(name="🔒 DRM", value=info["drm"], inline=False)
        if info.get("description"):
            embed.add_field(name="ℹ️ Info", value=info["description"], inline=False)
        embed.add_field(name="Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})")
        if info.get("header"): embed.set_image(url=info["header"])
        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed)

        tmp = f"/tmp/{file_name}"
        r = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers={"Authorization": f"Bearer {creds.token}"}, stream=True, timeout=120
        )
        with open(tmp, "wb") as out:
            for chunk in r.iter_content(1 << 14):
                if chunk: out.write(chunk)

        await interaction.followup.send(
            content="📥 File manifest hanya untuk requester:",
            file=discord.File(tmp, file_name),
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")

# ================= BACKGROUND =================
@tasks.loop(minutes=2)
async def check_new_files():
    global known_files
    try:
        items = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute().get("files", [])
        for f in items:
            fname, fid, mtime, fsize = f["name"], f["id"], f.get("modifiedTime",""), f.get("size","0")
            appid = fname.replace(".zip", "")
            info = await fetch_steam_info(appid)
            color = discord.Color.red() if info.get("drm") else discord.Color.purple()

            # New
            if fname not in known_files:
                known_files[fname] = {"id": fid,"mtime": mtime,"size": fsize}
                for g, conf in config.items():
                    ch = bot.get_channel(conf.get("upload_channel"))
                    if ch:
                        embed = discord.Embed(title="🆕 New Game Added", description=f"**{info['name']}** (`{appid}`)", color=color)
                        if info.get("header"): embed.set_image(url=info["header"])
                        if info.get("drm"): embed.add_field(name="🔒 DRM", value=info["drm"])
                        await ch.send(embed=embed)

            # Update
            elif known_files[fname]["size"] != fsize:
                known_files[fname] = {"id": fid,"mtime": mtime,"size": fsize}
                for g, conf in config.items():
                    ch = bot.get_channel(conf.get("update_channel"))
                    if ch:
                        embed = discord.Embed(title="♻️ Game Updated", description=f"**{info['name']}** (`{appid}`)", color=color)
                        embed.add_field(name="Update Date", value=mtime[:10])
                        if info.get("header"): embed.set_image(url=info["header"])
                        if info.get("drm"): embed.add_field(name="🔒 DRM", value=info["drm"])
                        await ch.send(embed=embed)
    except Exception as e:
        print("Loop error:", e)

@tasks.loop(hours=1)
async def check_patches():
    global known_patches
    try:
        for fname in known_files:
            appid = fname.replace(".zip", "")
            patch = await fetch_latest_patch(appid)
            if not patch: continue
            pid = str(patch.get("id"))
            if known_patches.get(appid) == pid: continue
            known_patches[appid] = pid

            info = await fetch_steam_info(appid)
            for g, conf in config.items():
                ch = bot.get_channel(conf.get("patch_channel"))
                if ch:
                    embed = discord.Embed(
                        title="🛠 Patch Update Detected",
                        description=f"**{info['name']}** (`{appid}`)",
                        color=discord.Color.orange()
                    )
                    embed.add_field(name="Patch Title", value=patch.get("title","N/A"), inline=False)
                    embed.add_field(name="Date", value=patch.get("time","")[:10], inline=True)
                    embed.add_field(name="Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
                    if info.get("header"): embed.set_image(url=info["header"])
                    if info.get("drm"): embed.add_field(name="🔒 DRM", value=info["drm"])
                    await ch.send(embed=embed)
    except Exception as e:
        print("Patch error:", e)

# ================= COMMANDS =================
@tree.command(name="notif", description="Aktif/Nonaktif monitor Drive")
async def notif(inter, mode: str):
    global ENABLE_UPLOAD_WATCH
    if mode.lower() == "on":
        ENABLE_UPLOAD_WATCH = True
        initialize_known_files()
        await inter.response.send_message("🔔 Drive Notif **ON**")
    else:
        ENABLE_UPLOAD_WATCH = False
        await inter.response.send_message("🔕 Drive Notif **OFF**")

@tree.command(name="channeluploadsetup")
async def channeluploadsetup(inter, channel: discord.TextChannel):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await inter.response.send_message(f"✅ Upload channel → {channel.mention}")

@tree.command(name="channelupdatesetup")
async def channelupdatesetup(inter, channel: discord.TextChannel):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await inter.response.send_message(f"✅ Update channel → {channel.mention}")

@tree.command(name="channelrequestsetup")
async def channelrequestsetup(inter, channel: discord.TextChannel, role: discord.Role=None):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["request_channel"] = channel.id
    config[str(inter.guild_id)]["request_role"] = role.id if role else None
    save_config(config)
    await inter.response.send_message(f"✅ Request channel → {channel.mention} | Role: {role.mention if role else 'None'}")

@tree.command(name="channelpatchsetup")
async def channelpatchsetup(inter, channel: discord.TextChannel):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["patch_channel"] = channel.id
    save_config(config)
    await inter.response.send_message(f"✅ Patch channel → {channel.mention}")

# ================= READY =================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot logged in as {bot.user}")
    initialize_known_files()
    check_new_files.start()
    check_patches.start()

# ================= START =================
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
