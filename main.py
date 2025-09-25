import os
import json
import time
import traceback
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build
import aiohttp
import requests
from typing import Optional

# =============== KEEP-ALIVE (OPTIONAL) ===============
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    # Bind ke PORT dari env (Render)
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# =============== DISCORD SETUP ===============
intents = discord.Intents.default()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")  # Google Drive folder ID

# =============== GOOGLE DRIVE SETUP ===============
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")
if not SERVICE_ACCOUNT_INFO:
    raise ValueError("GDRIVE_CREDENTIALS environment variable is required")

creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# =============== CONFIG PERSISTENCE (per-guild) ===============
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

# =============== DRIVE CACHE (anti-spam) ===============
known_files = {}
ENABLE_UPLOAD_WATCH = False

# anti-spam Added
NOTIFIED_FILE = "notified.json"

def load_notified():
    if os.path.exists(NOTIFIED_FILE):
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_notified(data):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(data), f, indent=2)

notified_files = load_notified()

def initialize_known_files():
    global known_files
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute()
        items = results.get("files", [])
        known_files = {
            f["name"]: {
                "id": f["id"],
                "mtime": f.get("modifiedTime", ""),
                "ctime": f.get("createdTime", ""),
                "size": f.get("size", "0")
            }
            for f in items
        }
        print(f"Initialized cache: {len(known_files)} files.")
    except Exception as e:
        print("Error initializing known_files:", e)

def count_manifests_in_cache(appid: str):
    """
    Count number of manifest files in cache for a given appid.
    We consider files whose name starts with '{appid}' or contains '{appid}.zip'
    """
    prefix = f"{appid}"
    count = 0
    for name in known_files.keys():
        if name.startswith(prefix) or f"{prefix}.zip" in name:
            count += 1
    return count

# =============== STEAM INFO HELPER ===============
async def fetch_steam_info(appid: str):
    """Fetch store API details (best-effort)."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
                entry = data.get(str(appid), {})
                if entry.get("success"):
                    g = entry["data"]
                    devs = g.get("developers") or []
                    dev = devs[0] if devs else None
                    release_date = g.get("release_date", {}).get("date") or None
                    return {
                        "name": g.get("name", f"AppID {appid}"),
                        "header": g.get("header_image"),
                        "steam": f"https://store.steampowered.com/app/{appid}",
                        "steamdb": f"https://steamdb.info/app/{appid}",
                        "release_date": release_date,
                        "developer": dev,
                        "description": g.get("short_description", "")[:800]
                    }
    except Exception:
        pass
    return {
        "name": f"AppID {appid}",
        "header": None,
        "steam": f"https://store.steampowered.com/app/{appid}",
        "steamdb": f"https://steamdb.info/app/{appid}",
        "release_date": None,
        "developer": None,
        "description": ""
    }

# === Multi-CDN resolver untuk header image (pakai link langsung, bukan attachment) ===
def resolve_header_url(appid: str, hinted_url: Optional[str]) -> Optional[str]:
    """
    Pilih URL header paling sehat (200 OK, content-type image) dari beberapa CDN Steam.
    Urutan: hinted_url (jika ada) -> Fastly -> Cloudflare -> Akamai.
    """
    candidates = []
    if hinted_url:
        candidates.append(hinted_url)
    candidates.extend([
        f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://steamcdn-a.akamaihd.net/steam/apps/{appid}/header.jpg",
    ])

    for url in candidates:
        try:
            # HEAD cepat; beberapa edge nolak HEAD -> fallback GET
            resp = requests.head(url, timeout=4, allow_redirects=True)
            ct = resp.headers.get("Content-Type", "").lower()
            if resp.status_code == 200 and "image" in ct:
                return url
            if resp.status_code in (403, 404, 405) or "image" not in ct:
                r2 = requests.get(url, timeout=6, stream=True)
                ct2 = r2.headers.get("Content-Type", "").lower()
                if r2.status_code == 200 and "image" in ct2:
                    r2.close()
                    return url
                r2.close()
        except Exception:
            continue
    return hinted_url

# =============== /gen COMMAND (non-owner allowed) ===============
DISCORD_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024  # ~8MB (server non-boost)

def ensure_public_link(file_id: str):
    """Pastikan file bisa diakses via link publik. Return (webContentLink, webViewLink)."""
    try:
        meta = drive_service.files().get(
            fileId=file_id, fields="webContentLink, webViewLink, permissions"
        ).execute()
        web_content = meta.get("webContentLink")
        web_view = meta.get("webViewLink")

        perms = meta.get("permissions", [])
        is_public = any(p.get("type") == "anyone" for p in perms)
        if not is_public:
            try:
                drive_service.permissions().create(
                    fileId=file_id,
                    body={"type": "anyone", "role": "reader"},
                ).execute()
                meta = drive_service.files().get(
                    fileId=file_id, fields="webContentLink, webViewLink"
                ).execute()
                web_content = meta.get("webContentLink")
                web_view = meta.get("webViewLink")
            except Exception:
                pass
        return web_content, web_view
    except Exception:
        return None, None

@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    if interaction.guild_id:
        ensure_guild_config(interaction.guild_id)

    await interaction.response.defer(ephemeral=False)  # jaga agar interaction tidak expired
    start_t = time.perf_counter()

    try:
        # 1) Cari file di Drive
        q = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(
            q=q,
            fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute()
        items = results.get("files", [])

        # 2) Jika tidak ditemukan → kirim embed "Requested (Not Found)"
        if not items:
            info = await fetch_steam_info(appid)
            embed_nf = discord.Embed(
                title="🚨 Game Requested (Not Found)",
                description=f"User {interaction.user.mention} request AppID **{appid}**",
                color=discord.Color.red()
            )
            embed_nf.add_field(name="🔗 Steam", value=f"[Open]({info['steam']})", inline=True)
            embed_nf.add_field(name="📊 SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
            if info.get("developer"):
                embed_nf.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
            if info.get("release_date"):
                embed_nf.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
            header_url = resolve_header_url(appid, info.get("header"))
            if header_url:
                embed_nf.set_image(url=header_url)
            embed_nf.timestamp = discord.utils.utcnow()
            embed_nf.set_footer(text="Requested via /gen")

            await interaction.followup.send(embed=embed_nf, ephemeral=False)

            conf = config.get(str(interaction.guild_id), {})
            req_ch = conf.get("request_channel")
            req_role = conf.get("request_role")
            if req_ch:
                ch = bot.get_channel(req_ch)
                if ch:
                    mention_txt = f"<@&{req_role}>" if req_role else None
                    if mention_txt:
                        await ch.send(content=mention_txt, embed=embed_nf)
                    else:
                        await ch.send(embed=embed_nf)
            return

        # 3) Found → siapkan embed publik dulu (supaya user langsung lihat hasil)
        f = items[0]
        file_id, file_name = f["id"], f["name"]
        created = f.get("createdTime", "")
        modified = f.get("modifiedTime", "")
        size_bytes = int(f.get("size", 0))
        size_kb = size_bytes // 1024

        info = await fetch_steam_info(appid)
        elapsed = time.perf_counter() - start_t
        release_date = info.get("release_date") or created[:10] or modified[:10]

        embed = discord.Embed(title="✅ Manifest Retrieved", color=discord.Color.purple())
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)
        embed.add_field(name="📅 Release Date", value=release_date, inline=True)
        if info.get("developer"):
            embed.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
        embed.add_field(name="⏱️ Time", value=f"{elapsed:.2f}s", inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        embed.add_field(name="📥 Download", value="File hanya bisa diunduh oleh requester (lihat bawah).", inline=False)
        if info.get("description"):
            embed.add_field(name="ℹ️ Info", value=info["description"], inline=False)
        header_url = resolve_header_url(appid, info.get("header"))
        if header_url:
            embed.set_image(url=header_url)
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed, ephemeral=False)

        # 4) Jika file > batas upload Discord → langsung kirim link Drive (tanpa download)
        if size_bytes > DISCORD_UPLOAD_LIMIT_BYTES:
            dl_link, view_link = ensure_public_link(file_id)
            link_text = dl_link or view_link or f"https://drive.google.com/file/d/{file_id}/view"
            await interaction.followup.send(
                content=f"⚠️ File terlalu besar untuk diupload ke Discord.\n🔗 **Download:** {link_text}",
                ephemeral=True
            )
            return

        # 5) File masih dalam batas → download streaming dengan timeout aman
        tmp_path = f"/tmp/{file_name}"
        try:
            with requests.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                headers={"Authorization": f"Bearer {creds.token}"},
                stream=True,
                timeout=(15, 60),  # (connect timeout, read timeout)
            ) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as out_f:
                    for chunk in r.iter_content(chunk_size=1 << 14):
                        if chunk:
                            out_f.write(chunk)
        except Exception as dl_err:
            traceback.print_exc()
            dl_link, view_link = ensure_public_link(file_id)
            link_text = dl_link or view_link or f"https://drive.google.com/file/d/{file_id}/view"
            await interaction.followup.send(
                content=f"⚠️ Gagal mengunduh file dari Drive (akan dikirim link langsung).\n🔗 **Download:** {link_text}\n📝 Detail: `{dl_err}`",
                ephemeral=True
            )
            return

        # 6) Kirim file ke requester (ephemeral)
        await interaction.followup.send(
            content="📥 File manifest siap diunduh:",
            file=discord.File(tmp_path, file_name),
            ephemeral=True
        )

    except Exception as e:
        traceback.print_exc()
        try:
            await interaction.followup.send(f"⚠️ Error saat menjalankan /gen: {e}", ephemeral=True)
        except Exception:
            pass

# =============== BACKGROUND MONITOR ===============
@tasks.loop(minutes=1)
async def check_new_files():
    global known_files, ENABLE_UPLOAD_WATCH, notified_files
    if not ENABLE_UPLOAD_WATCH:
        return
    try:
        results = drive_service.files().list(q=f"'{FOLDER_ID}' in parents", fields="files(id,name,createdTime,modifiedTime,size)").execute()
        items = results.get("files", [])

        for f in items:
            fname = f["name"]
            fid = f["id"]
            mtime = f.get("modifiedTime", "")
            ctime = f.get("createdTime", "")
            fsize = f.get("size", "0")
            appid = fname.replace(".zip", "")
            info = await fetch_steam_info(appid)

            # NEW
            if fname not in known_files:
                known_files[fname] = {"id": fid, "mtime": mtime, "ctime": ctime, "size": fsize}
                # anti-spam Added
                if fname in notified_files:
                    continue
                notified_files.add(fname)
                save_notified(notified_files)

                total_files = count_manifests_in_cache(appid)
                for gid, conf in list(config.items()):
                    ch_id = conf.get("upload_channel")
                    if ch_id and (ch := bot.get_channel(ch_id)):
                        embed = discord.Embed(
                            title=f"🆕 New Game Added — {info['name']} ({appid})",
                            description=f"**{info['name']}** (`{appid}`) ditambahkan ke drive.",
                            color=discord.Color.blue()
                        )
                        if info.get("developer"): embed.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
                        if info.get("release_date"): embed.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
                        embed.add_field(name="📦 Manifest Files", value=str(total_files), inline=True)
                        embed.add_field(name="📅 Upload Date", value=ctime[:10], inline=True)
                        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
                        header_url = resolve_header_url(appid, info.get("header"))
                        if header_url: embed.set_image(url=header_url)
                        embed.timestamp = discord.utils.utcnow()
                        embed.set_footer(text="Reported by TechStation Manifest")
                        await ch.send(embed=embed)

            # UPDATED
            else:
                if (known_files[fname]["size"] != fsize) or (known_files[fname]["mtime"] != mtime):
                    known_files[fname] = {"id": fid, "mtime": mtime, "ctime": ctime, "size": fsize}
                    total_files = count_manifests_in_cache(appid)
                    for gid, conf in list(config.items()):
                        ch_id = conf.get("update_channel")
                        if ch_id and (ch := bot.get_channel(ch_id)):
                            embed = discord.Embed(
                                title=f"♻️ Game Updated — {info['name']} ({appid})",
                                description=f"**{info['name']}** (`{appid}`) diperbarui (file size berubah).",
                                color=discord.Color.orange()
                            )
                            if info.get("developer"): embed.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
                            if info.get("release_date"): embed.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
                            embed.add_field(name="📦 Manifest Files", value=str(total_files), inline=True)
                            embed.add_field(name="📅 Upload Date", value=known_files[fname].get("ctime", "")[:10], inline=True)
                            embed.add_field(name="🔁 Update Date", value=mtime[:10], inline=True)
                            embed.add_field(name="📦 New Size", value=f"{int(fsize)//1024} KB", inline=True)
                            embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
                            header_url = resolve_header_url(appid, info.get("header"))
                            if header_url: embed.set_image(url=header_url)
                            embed.timestamp = discord.utils.utcnow()
                            embed.set_footer(text="Reported by TechStation Manifest")
                            await ch.send(embed=embed)

    except Exception as e:
        print("check_new_files error:", e)

# =============== OWNER-ONLY GUARD ===============
def _owner_only(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id

# =============== /notif on|off (OWNER ONLY) ===============
@tree.command(name="notif", description="🔔 Aktif/Nonaktif monitor Drive (added/updated)")
async def notif(interaction: discord.Interaction, mode: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Hanya owner server yang boleh pakai command ini.", ephemeral=True)
        return
    global ENABLE_UPLOAD_WATCH
    m = mode.lower()
    await interaction.response.defer(ephemeral=True)
    if m == "on":
        ENABLE_UPLOAD_WATCH = True
        initialize_known_files()
        await interaction.followup.send("🔔 Notifikasi Drive: **AKTIF**")
    elif m == "off":
        ENABLE_UPLOAD_WATCH = False
        await interaction.followup.send("🔕 Notifikasi Drive: **NONAKTIF**")
    else:
        await interaction.followup.send("Gunakan `/notif on` atau `/notif off`")

# =============== channel setup (OWNER ONLY) ===============
@tree.command(name="channeluploadsetup", description="📌 Set channel untuk notif file baru (Added)")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Hanya owner server yang boleh pakai command ini.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await interaction.followup.send(f"✅ Channel Added diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="📌 Set channel untuk notif file update")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Hanya owner server yang boleh pakai command ini.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await interaction.followup.send(f"✅ Channel Updated diset ke {channel.mention}")

@tree.command(name="channelrequestsetup", description="📌 Set channel + role mention untuk request (Not Found)")
async def channelrequestsetup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Hanya owner server yang boleh pakai command ini.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["request_channel"] = channel.id
    config[str(interaction.guild_id)]["request_role"] = role.id if role else None
    save_config(config)
    txt = f"✅ Channel Request diset ke {channel.mention}"
    if role:
        txt += f" dan role mention diset ke {role.mention}"
    await interaction.followup.send(txt)

# =============== ON READY ===============
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot logged in as {bot.user} — in {len(bot.guilds)} guilds")
    initialize_known_files()
    check_new_files.start()

# =============== START ===============
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
