import os
import json
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build
import aiohttp
import requests

# =========================
# KEEP-ALIVE (optional)
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()

# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")  # folder di My Drive yang dipakai bot

# =========================
# GOOGLE DRIVE (Service Account)
# =========================
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")
if not SERVICE_ACCOUNT_INFO:
    raise ValueError("❌ GDRIVE_CREDENTIALS tidak ditemukan di environment variable!")

creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# =========================
# CONFIG PERSISTENCE (per-guild)
# =========================
CONFIG_FILE = "bot_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

config = load_config()   # { "guild_id_str": {"upload_channel": int|None, "update_channel": int|None} }

def ensure_guild_config(guild_id: int):
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {"upload_channel": None, "update_channel": None}
        save_config(config)

# =========================
# CACHE DRIVE (anti spam)
# =========================
# { filename: {"id": "...", "mtime": "...", "size": "12345"} }
known_files = {}
ENABLE_UPLOAD_WATCH = False

def initialize_known_files():
    """Seed cache dari isi folder Drive agar gak spam waktu notif dinyalakan."""
    global known_files
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id, name, modifiedTime, size)"
        ).execute()
        items = results.get("files", [])
        known_files = {
            f["name"]: {
                "id": f["id"],
                "mtime": f.get("modifiedTime", ""),
                "size": f.get("size", "0")
            }
            for f in items
        }
        print(f"🔹 Initialized cache: {len(known_files)} file.")
    except Exception as e:
        print(f"❌ Error init known files: {e}")

# =========================
# STEAM INFO
# =========================
async def fetch_steam_info(appid: str):
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get(str(appid), {}).get("success"):
                    g = data[str(appid)]["data"]
                    return {
                        "name": g.get("name", f"AppID {appid}"),
                        "header": g.get("header_image", None),
                        "steam": f"https://store.steampowered.com/app/{appid}",
                        "steamdb": f"https://steamdb.info/app/{appid}"
                    }
    except:
        pass
    return {
        "name": f"AppID {appid}",
        "header": None,
        "steam": f"https://store.steampowered.com/app/{appid}",
        "steamdb": f"https://steamdb.info/app/{appid}"
    }

# =========================
# /gen command
# - Post info publik ke channel
# - ZIP dikirim private via DM ke requester
# =========================
@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    ensure_guild_config(interaction.guild_id)
    # defer publik: supaya embed info bisa dilihat semua orang
    await interaction.response.defer(ephemeral=False)

    try:
        query = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(
            q=query,
            fields="files(id, name, createdTime, modifiedTime, size)"
        ).execute()
        items = results.get("files", [])

        if not items:
            # beri tahu publik bahwa file tidak ada
            await interaction.followup.send(f"❌ File untuk AppID {appid} tidak ditemukan.")
            # opsional: post juga ke channel "update" server ini sebagai log request not found
            up_ch = config[str(interaction.guild_id)]["update_channel"]
            if up_ch:
                ch = bot.get_channel(up_ch)
                if ch:
                    info = await fetch_steam_info(appid)
                    embed = discord.Embed(
                        title="📌 Game Requested (Not Found)",
                        description=f"User {interaction.user.mention} request AppID **{appid}**",
                        color=discord.Color.purple()
                    )
                    embed.add_field(name="Steam Store", value=f"[Open]({info['steam']})", inline=True)
                    embed.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
                    embed.set_footer(text="Requested via /gen")
                    await ch.send(embed=embed)
            return

        file = items[0]
        file_id = file["id"]
        file_name = file["name"]
        created = file.get("createdTime", "")
        modified = file.get("modifiedTime", "")
        size_kb = int(file.get("size", 0)) // 1024

        info = await fetch_steam_info(appid)

        # Embed publik (info requester + info game)
        embed_pub = discord.Embed(
            title="✅ Manifest Requested",
            description=f"Diminta oleh {interaction.user.mention}",
            color=discord.Color.purple()
        )
        embed_pub.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed_pub.add_field(name="🆔 AppID", value=appid, inline=True)
        embed_pub.add_field(name="📦 Size", value=f"{size_kb} KB", inline=True)
        embed_pub.add_field(name="📅 Upload", value=created[:10], inline=True)
        embed_pub.add_field(name="♻️ Update", value=modified[:10], inline=True)
        embed_pub.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        if info["header"]:
            embed_pub.set_image(url=info["header"])
        embed_pub.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed_pub)  # publik

        # Unduh ZIP ke temp dan kirim PRIVATE via DM
        try:
            filepath = f"/tmp/{file_name}"
            r = requests.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                headers={"Authorization": f"Bearer {creds.token}"},
                stream=True,
                timeout=60
            )
            with open(filepath, "wb") as f_out:
                for chunk in r.iter_content(chunk_size=1 << 14):
                    if chunk:
                        f_out.write(chunk)

            # kirim via DM
            dm_text = f"📥 **{info['name']}** (`{appid}`)\nBerikut file manifest yang kamu minta:"
            await interaction.user.send(content=dm_text, file=discord.File(filepath, file_name))
        except discord.Forbidden:
            # DM tertutup — fallback kirim ephemerally ke requester
            await interaction.followup.send(
                content="⚠️ DM kamu tertutup. Aktifkan DM atau izinkan pesan dari server ini.\nSebagai alternatif, file dikirim privat di sini:",
                ephemeral=True
            )
            # kirim file ephemeral (hanya requester yang melihat)
            filepath = f"/tmp/{file_name}"
            if not os.path.exists(filepath):
                r = requests.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                    headers={"Authorization": f"Bearer {creds.token}"},
                    stream=True,
                    timeout=60
                )
                with open(filepath, "wb") as f_out:
                    for chunk in r.iter_content(chunk_size=1 << 14):
                        if chunk:
                            f_out.write(chunk)
            await interaction.followup.send(file=discord.File(filepath, file_name), ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error saat generate: {e}")

# =========================
# BACKGROUND: monitor Drive
# - Added -> kirim ke upload_channel
# - Updated (size berubah) -> kirim ke update_channel
# =========================
@tasks.loop(minutes=1)
async def check_new_files():
    global known_files, ENABLE_UPLOAD_WATCH
    if not ENABLE_UPLOAD_WATCH:
        return
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id, name, createdTime, modifiedTime, size)"
        ).execute()
        items = results.get("files", [])

        for f in items:
            fname = f["name"]
            fid = f["id"]
            mtime = f.get("modifiedTime", "")
            fsize = f.get("size", "0")
            appid = fname.replace(".zip", "")
            info = await fetch_steam_info(appid)

            # Baru
            if fname not in known_files:
                known_files[fname] = {"id": fid, "mtime": mtime, "size": fsize}
                # broadcast ke semua guild yang punya upload_channel
                for gid, conf in list(config.items()):
                    ch_id = conf.get("upload_channel")
                    if ch_id:
                        ch = bot.get_channel(ch_id)
                        if ch:
                            embed = discord.Embed(
                                title="🆕 New Game Added",
                                description=f"**{info['name']}** (`{appid}`) ditambahkan.",
                                color=discord.Color.purple()
                            )
                            embed.add_field(name="Upload Date", value=f.get("createdTime","")[:10], inline=True)
                            if info["header"]:
                                embed.set_thumbnail(url=info["header"])
                            await ch.send(embed=embed)

            # Sudah ada -> cek size berubah (anti-spam)
            else:
                if known_files[fname]["size"] != fsize:
                    known_files[fname] = {"id": fid, "mtime": mtime, "size": fsize}
                    for gid, conf in list(config.items()):
                        ch_id = conf.get("update_channel")
                        if ch_id:
                            ch = bot.get_channel(ch_id)
                            if ch:
                                embed = discord.Embed(
                                    title="♻️ Game Updated",
                                    description=f"**{info['name']}** (`{appid}`) diperbarui (size berubah).",
                                    color=discord.Color.purple()
                                )
                                embed.add_field(name="Update Date", value=mtime[:10], inline=True)
                                embed.add_field(name="New Size", value=f"{int(fsize)//1024} KB", inline=True)
                                if info["header"]:
                                    embed.set_thumbnail(url=info["header"])
                                await ch.send(embed=embed)

    except Exception as e:
        print(f"❌ check_new_files error: {e}")

# =========================
# /notif on|off
# =========================
@tree.command(name="notif", description="Aktif/Nonaktif monitor Drive (added/updated)")
async def notif(interaction: discord.Interaction, mode: str):
    global ENABLE_UPLOAD_WATCH
    mode = mode.lower()
    if mode == "on":
        ENABLE_UPLOAD_WATCH = True
        initialize_known_files()
        await interaction.response.send_message("🔔 Notifikasi Drive: **AKTIF** (added & updated).")
    elif mode == "off":
        ENABLE_UPLOAD_WATCH = False
        await interaction.response.send_message("🔕 Notifikasi Drive: **NONAKTIF**.")
    else:
        await interaction.response.send_message("❌ Gunakan: `/notif on` atau `/notif off`")

# =========================
# /channeluploadsetup & /channelupdatesetup (per guild)
# =========================
@tree.command(name="channeluploadsetup", description="Set channel untuk notif file baru (Added)")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Added** diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="Set channel untuk notif file update (size berubah)")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Updated** diset ke {channel.mention}")

# =========================
# READY
# =========================
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot {bot.user} online di {len(bot.guilds)} server.")
    initialize_known_files()
    check_new_files.start()

# =========================
# START
# =========================
keep_alive()
bot.run(DISCORD_TOKEN)
