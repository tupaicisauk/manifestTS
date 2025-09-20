import os, json, requests
import discord
from discord import app_commands
from discord.ext import commands, tasks
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ===== DISCORD SETUP =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# ===== GOOGLE DRIVE SETUP =====
def get_google_service():
    creds = Credentials(
        token=os.getenv("GOOGLE_ACCESS_TOKEN"),
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

# ===== STORAGE FILE (tracking update) =====
SEEN_FILE = "seen.json"
if not os.path.exists(SEEN_FILE):
    with open(SEEN_FILE, "w") as f: json.dump({}, f)

def load_seen(): return json.load(open(SEEN_FILE))
def save_seen(d): json.dump(d, open(SEEN_FILE, "w"), indent=2)

# ===== DISCORD EMBED REPORT =====
async def send_report(channel, filename, status, mtime):
    appid = filename.replace(".zip","")
    color = 0x00ff00 if status == "Added" else 0xffa500
    embed = discord.Embed(
        title=f"{status}: {appid}",
        description=f"Manifest `{filename}`",
        color=color
    )
    embed.add_field(
        name="Links",
        value=f"[Steam Store](https://store.steampowered.com/app/{appid}) | "
              f"[SteamDB](https://steamdb.info/app/{appid}/)",
        inline=False
    )
    embed.add_field(name="Updated", value=mtime, inline=True)
    await channel.send(embed=embed)

# ===== LOOP CHECK GOOGLE DRIVE =====
@tasks.loop(minutes=5)
async def check_drive():
    service = get_google_service()
    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents",
        fields="files(id, name, modifiedTime)"
    ).execute()
    files = results.get("files", [])

    seen = load_seen()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)

    for f in files:
        name, mtime = f["name"], f["modifiedTime"]
        if name not in seen:
            await send_report(channel, name, "Added", mtime)
        elif seen[name] != mtime:
            await send_report(channel, name, "Updated", mtime)
        seen[name] = mtime

    save_seen(seen)

# ===== SLASH COMMAND /gen =====
@bot.tree.command(name="gen", description="Generate manifest dari Google Drive")
@app_commands.describe(appid="Masukkan AppID game")
async def gen(interaction: discord.Interaction, appid: str):
    await interaction.response.defer()
    service = get_google_service()
    query = f"name='{appid}.zip' and '{FOLDER_ID}' in parents"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if not files:
        await interaction.followup.send(f"❌ Manifest {appid} not found.")
        return

    file_id = files[0]["id"]
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    r = requests.get(download_url)

    temp_path = f"/tmp/{appid}.zip"
    with open(temp_path, "wb") as f:
        f.write(r.content)

    embed = discord.Embed(
        title=f"✅ Manifest Generated: AppID {appid}",
        description=f"Successfully generated manifest for `{appid}`",
        color=0x2ecc71
    )
    await interaction.followup.send(embed=embed, file=discord.File(temp_path))

# ===== BOT EVENT =====
@bot.event
async def on_ready():
    print(f"✅ Bot {bot.user} online!")
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print("Error syncing commands:", e)
    check_drive.start()

# ===== RUN BOT =====
bot.run(DISCORD_TOKEN)
