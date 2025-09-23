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

# ====== KEEP ALIVE SERVER ======
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ====== DISCORD BOT ======
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")

# ====== GOOGLE DRIVE SETUP ======
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")

if not SERVICE_ACCOUNT_INFO:
    raise ValueError("❌ GDRIVE_CREDENTIALS tidak ditemukan di environment variable!")

creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# ====== CONFIG PERSISTENT ======
CONFIG_FILE = "bot_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"upload_channel": None, "request_channel": None}

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

config = load_config()
UPLOAD_CHANNEL_ID = config.get("upload_channel")
REQUEST_CHANNEL_ID = config.get("request_channel")

# ====== CACHE ======
known_files = {}
ENABLE_UPLOAD_WATCH = False

# ====== INIT CACHE ======
def initialize_known_files():
    global known_files
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id, name, modifiedTime)"
        ).execute()
        items = results.get("files", [])
        known_files = {f["name"]: {"id": f["id"], "mtime": f["modifiedTime"]} for f in items}
        print(f"🔹 Initialized {len(known_files)} file ke cache.")
    except Exception as e:
        print(f"❌ Error init known files: {e}")

# ====== FETCH DATA DARI STEAM ======
async def fetch_steam_info(appid: str):
    try:
        async with aiohttp.ClientSession() as session:
            store_url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(store_url) as resp:
                data = await resp.json()
                if data[str(appid)]["success"]:
                    g = data[str(appid)]["data"]
                    return {
                        "name": g["name"],
                        "header": g["header_image"],
                        "steam": f"https://store.steampowered.com/app/{appid}",
                        "steamdb": f"https://steamdb.info/app/{appid}"
                    }
        return {"name": f"AppID {appid}", "header": None,
                "steam": f"https://store.steampowered.com/app/{appid}",
                "steamdb": f"https://steamdb.info/app/{appid}"}
    except:
        return {"name": f"AppID {appid}", "header": None,
                "steam": f"https://store.steampowered.com/app/{appid}",
                "steamdb": f"https://steamdb.info/app/{appid}"}

# ====== SLASH COMMAND /gen ======
@tree.command(name="gen", description="Generate manifest dari Google Drive dengan AppID")
async def gen(interaction: discord.Interaction, appid: str):
    await interaction.response.defer(ephemeral=True)

    try:
        query = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(
            q=query,
            fields="files(id, name, createdTime, modifiedTime, size)"
        ).execute()
        items = results.get("files", [])

        if not items:
            await interaction.followup.send(f"❌ File untuk AppID {appid} tidak ditemukan.", ephemeral=True)

            # kirim notif ke request_channel
            if REQUEST_CHANNEL_ID:
                channel = bot.get_channel(REQUEST_CHANNEL_ID)
                if channel:
                    info = await fetch_steam_info(appid)
                    embed = discord.Embed(
                        title="📌 Game Requested (Not Found)",
                        description=f"User `{interaction.user}` request AppID **{appid}**",
                        color=discord.Color.purple()
                    )
                    embed.add_field(name="Steam Store", value=f"[Open]({info['steam']})", inline=True)
                    embed.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
                    embed.set_footer(text="Requested via /gen")
                    await channel.send(embed=embed)
            return

        file = items[0]
        file_id, file_name = file["id"], file["name"]
        created, modified = file["createdTime"], file["modifiedTime"]
        size = int(file.get("size", 0)) // 1024  # KB
        info = await fetch_steam_info(appid)

        # download file zip sementara
        filepath = f"/tmp/{file_name}"
        downloader = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers={"Authorization": f"Bearer {creds.token}"},
            stream=True
        )
        with open(filepath, "wb") as f:
            for chunk in downloader.iter_content(chunk_size=4096):
                if chunk:
                    f.write(chunk)

        # embed style keren
        embed = discord.Embed(
            title="✅ Manifest Retrieved",
            description=f"Game manifest berhasil diambil!",
            color=discord.Color.purple()
        )
        embed.add_field(name="🎮 Game", value=info['name'], inline=True)
        embed.add_field(name="🆔 Steam ID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size} KB", inline=True)
        embed.add_field(name="📅 Upload", value=created[:10], inline=True)
        embed.add_field(name="♻️ Update", value=modified[:10], inline=True)
        embed.add_field(name="🔗 Links", value=f"[Steam Store]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        embed.add_field(name="🙋 Requester", value=interaction.user.mention, inline=True)

        if info['header']:
            embed.set_image(url=info['header'])
        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed, file=discord.File(filepath, file_name), ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error saat generate manifest: {str(e)}", ephemeral=True)

# ====== BACKGROUND TASK CEK FILE ======
@tasks.loop(minutes=1)
async def check_new_files():
    global known_files, ENABLE_UPLOAD_WATCH, UPLOAD_CHANNEL_ID
    if not ENABLE_UPLOAD_WATCH or not UPLOAD_CHANNEL_ID:
        return

    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id, name, createdTime, modifiedTime)"
        ).execute()
        items = results.get("files", [])
        channel = bot.get_channel(UPLOAD_CHANNEL_ID)

        for f in items:
            fname, fid, mtime = f["name"], f["id"], f["modifiedTime"]

            if fname in known_files:
                if (known_files[fname]["id"] != fid) or (known_files[fname]["mtime"] != mtime):
                    known_files[fname] = {"id": fid, "mtime": mtime}
                    appid = fname.replace(".zip", "")
                    info = await fetch_steam_info(appid)
                    embed = discord.Embed(
                        title="♻️ Game Updated",
                        description=f"**{info['name']}** ({appid}) diperbarui.",
                        color=discord.Color.purple()
                    )
                    embed.add_field(name="Update Date", value=mtime[:10], inline=True)
                    if info['header']:
                        embed.set_thumbnail(url=info['header'])
                    await channel.send(embed=embed)
            else:
                known_files[fname] = {"id": fid, "mtime": mtime}
                appid = fname.replace(".zip", "")
                info = await fetch_steam_info(appid)
                embed = discord.Embed(
                    title="🆕 New Game Added",
                    description=f"**{info['name']}** ({appid}) ditambahkan.",
                    color=discord.Color.purple()
                )
                embed.add_field(name="Upload Date", value=f["createdTime"][:10], inline=True)
                if info['header']:
                    embed.set_thumbnail(url=info['header'])
                await channel.send(embed=embed)

    except Exception as e:
        print(f"❌ Error di check_new_files: {e}")

# ====== SLASH COMMAND NOTIF ======
@tree.command(name="notif", description="Aktifkan atau matikan auto-notif upload/update")
async def notif(interaction: discord.Interaction, mode: str):
    global ENABLE_UPLOAD_WATCH
    mode = mode.lower()
    if mode == "on":
        ENABLE_UPLOAD_WATCH = True
        initialize_known_files()
        await interaction.response.send_message("🔔 Notifikasi AKTIF (file baru/update).")
    elif mode == "off":
        ENABLE_UPLOAD_WATCH = False
        await interaction.response.send_message("🔕 Notifikasi DIMATIKAN.")
    else:
        await interaction.response.send_message("❌ Gunakan `/notif on` atau `/notif off`")

# ====== SLASH COMMAND SETUP CHANNEL ======
@tree.command(name="channeluploadsetup", description="Set channel untuk notif upload/update game")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    global UPLOAD_CHANNEL_ID, config
    UPLOAD_CHANNEL_ID = channel.id
    config["upload_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel upload/update diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="Set channel untuk notif request game yang belum ada")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    global REQUEST_CHANNEL_ID, config
    REQUEST_CHANNEL_ID = channel.id
    config["request_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel request-not-found diset ke {channel.mention}")

# ====== ON READY ======
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot {bot.user} sudah online!")
    initialize_known_files()
    check_new_files.start()

# ====== START BOT ======
keep_alive()
bot.run(DISCORD_TOKEN)
