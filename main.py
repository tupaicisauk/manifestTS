import os
import sys
import json
import time
import tempfile
import asyncio
import requests
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread

# =========================
#  Keep-Alive (Render)
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def _run_keepalive():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=_run_keepalive, daemon=True).start()


# =========================
#  ENV & Validation
# =========================
def must_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        sys.exit(f"❌ ENV '{key}' tidak ditemukan. Isi di Render → Environment Variables.")
    return val

DISCORD_TOKEN       = must_env("DISCORD_TOKEN")
DISCORD_CHANNEL_ID  = must_env("DISCORD_CHANNEL_ID")
GOOGLE_CLIENT_ID    = must_env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET= must_env("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN= must_env("GOOGLE_REFRESH_TOKEN")
# dukung dua nama variabel untuk folder
DRIVE_FOLDER_ID     = os.getenv("DRIVE_FOLDER_ID") or os.getenv("FOLDER_ID")
if not DRIVE_FOLDER_ID:
    sys.exit("❌ ENV 'DRIVE_FOLDER_ID' (atau 'FOLDER_ID') tidak ditemukan.")

try:
    DISCORD_CHANNEL_ID = int(DISCORD_CHANNEL_ID)
except ValueError:
    sys.exit("❌ DISCORD_CHANNEL_ID harus angka (Copy **Channel ID**, bukan Server ID).")


# =========================
#  Discord Bot Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True  # kalau nanti butuh prefix-commands

bot = commands.Bot(command_prefix="!", intents=intents)  # command_prefix tidak dipakai utk slash
SEEN_PATH = "seen.json"


# =========================
#  Helper: Google Drive
# =========================
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_DOWNLOAD_URL = "https://www.googleapis.com/drive/v3/files/{id}?alt=media"

def get_access_token() -> str:
    """Mint access_token via refresh_token (server-to-server)."""
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    r = requests.post(TOKEN_URL, data=data, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to refresh access_token: {r.status_code} {r.text}")
    return r.json()["access_token"]

def drive_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}

def list_folder_files() -> list[dict]:
    """
    Return list of files in folder with fields:
      id, name, modifiedTime, size, mimeType
    """
    files = []
    params = {
        "q": f"'{DRIVE_FOLDER_ID}' in parents",
        "fields": "nextPageToken, files(id,name,modifiedTime,size,mimeType)",
        "pageSize": 1000,
        "orderBy": "name_natural",
    }
    headers = drive_headers()
    next_token = None
    while True:
        if next_token:
            params["pageToken"] = next_token
        r = requests.get(DRIVE_FILES_URL, headers=headers, params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Drive list error: {r.status_code} {r.text}")
        data = r.json()
        files.extend(data.get("files", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return files

def find_file_by_name(appid: str) -> dict | None:
    """Find exact 'appid.zip' under the folder."""
    query = (
        f"name='{appid}.zip' and '{DRIVE_FOLDER_ID}' in parents and "
        f"mimeType!='application/vnd.google-apps.folder'"
    )
    params = {
        "q": query,
        "fields": "files(id,name,modifiedTime,size)",
        "pageSize": 1
    }
    r = requests.get(DRIVE_FILES_URL, headers=drive_headers(), params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Drive search error: {r.status_code} {r.text}")
    lst = r.json().get("files", [])
    return lst[0] if lst else None

def download_file_to_temp(file_id: str, filename: str) -> str:
    """Download Drive file to temp path and return path."""
    url = DRIVE_DOWNLOAD_URL.format(id=file_id)
    r = requests.get(url, headers=drive_headers(), stream=True, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Drive download error: {r.status_code} {r.text}")
    fd, tmp_path = tempfile.mkstemp(prefix="manifest_", suffix=f"_{filename}")
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return tmp_path


# =========================
#  Seen DB (Added/Updated)
# =========================
def load_seen() -> dict:
    if not os.path.exists(SEEN_PATH):
        return {}
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_seen(d: dict) -> None:
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


# =========================
#  Embeds / Reporting
# =========================
def steam_links(appid: str) -> str:
    return f"[Steam Store](https://store.steampowered.com/app/{appid}) | [SteamDB](https://steamdb.info/app/{appid}/)"

async def send_manifest_embed(channel: discord.abc.Messageable, status: str, fileobj: dict):
    """
    status: 'Added' or 'Updated'
    fileobj: dict from Drive (id, name, modifiedTime, size)
    """
    name = fileobj.get("name", "unknown.zip")
    appid = name[:-4] if name.endswith(".zip") else name

    color = 0x2ecc71 if status == "Added" else 0xf39c12
    embed = discord.Embed(
        title=f"{status}: {appid}",
        description=f"Manifest `{name}`",
        color=color
    )
    embed.add_field(name="Links", value=steam_links(appid), inline=False)
    embed.add_field(name="Updated", value=fileobj.get("modifiedTime", "—"), inline=True)
    size = fileobj.get("size")
    if size:
        try:
            size_mb = int(size) / (1024 * 1024)
            embed.add_field(name="Size", value=f"{size_mb:.2f} MB", inline=True)
        except Exception:
            pass

    await channel.send(embed=embed)


# =========================
#  Slash Command: /gen
# =========================
@bot.tree.command(name="gen", description="Kirim manifest zip berdasarkan AppID dari Google Drive")
@app_commands.describe(appid="Contoh: 814380")
async def gen(interaction: discord.Interaction, appid: str):
    await interaction.response.defer(thinking=True)
    try:
        f = find_file_by_name(appid)
        if not f:
            await interaction.followup.send(f"❌ Manifest `{appid}.zip` tidak ditemukan di Google Drive.")
            return

        tmp_path = download_file_to_temp(f["id"], f["name"])
        embed = discord.Embed(
            title=f"✅ Manifest Generated: AppID {appid}",
            description=f"Berhasil mengambil `{f['name']}` dari Google Drive.",
            color=0x2ecc71
        )
        embed.add_field(name="Links", value=steam_links(appid), inline=False)
        await interaction.followup.send(embed=embed, file=discord.File(tmp_path, filename=f["name"]))
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# Ping (prefix) opsional
@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("🏓 Pong! Bot aktif.")

# =========================
#  Background Watcher
# =========================
@tasks.loop(minutes=5)
async def watch_drive():
    """
    Cek folder Google Drive tiap 5 menit,
    kirim report Added/Updated ke channel.
    """
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        print("❌ Channel tidak ditemukan. Pastikan bot ada di server & DISCORD_CHANNEL_ID benar.")
        return

    try:
        files = list_folder_files()
        seen = load_seen()

        # Saat pertama kali: seed tanpa spam
        if not seen and files:
            print(f"Seeding seen.json dengan {len(files)} file (tanpa kirim notifikasi).")
            for f in files:
                seen[f["name"]] = f.get("modifiedTime", "")
            save_seen(seen)
            return

        changed = False
        for f in files:
            name = f["name"]
            mtime = f.get("modifiedTime", "")
            if name not in seen:
                # file baru
                await send_manifest_embed(channel, "Added", f)
                seen[name] = mtime
                changed = True
            elif seen[name] != mtime:
                # file di-update
                await send_manifest_embed(channel, "Updated", f)
                seen[name] = mtime
                changed = True

        if changed:
            save_seen(seen)

    except Exception as e:
        print(f"[watch_drive] error: {e}")


# =========================
#  Events
# =========================
@bot.event
async def on_ready():
    # sync slash command
    try:
        synced = await bot.tree.sync()
        print(f"✨ Slash commands synced: {len(synced)}")
    except Exception as e:
        print(f"Slash sync error: {e}")

    print(f"✅ Bot {bot.user} sudah online!")
    # start watcher
    if not watch_drive.is_running():
        watch_drive.start()


# =========================
#  Run
# =========================
def main():
    keep_alive()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
