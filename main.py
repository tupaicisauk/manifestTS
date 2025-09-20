import discord
from discord.ext import commands
from flask import Flask
from threading import Thread
import os
import requests

# ====== KEEP ALIVE SERVER ======
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ====== DISCORD BOT ======
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()  # Sinkronisasi slash command ke Discord
    print(f"✅ Bot {bot.user} sudah online dengan slash command!")

# ====== SLASH COMMAND /ping ======
@bot.tree.command(name="ping", description="Cek apakah bot online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓")

# ====== SLASH COMMAND /gen ======
@bot.tree.command(name="gen", description="Generate manifest untuk game berdasarkan AppID")
async def gen(interaction: discord.Interaction, appid: str):
    folder = "manifests"
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, f"{appid}.zip")

    # URL GitHub branch zip
    zip_url = f"https://github.com/SteamAutoCracks/ManifestHub/archive/refs/heads/{appid}.zip"

    # Default info
    game_name = f"AppID {appid}"
    steam_link = f"https://store.steampowered.com/app/{appid}/"
    steamdb_link = f"https://steamdb.info/app/{appid}/"
    cover_img = None

    # Coba ambil data dari Steam API
    try:
        steam_api = f"https://store.steampowered.com/api/appdetails?appids={appid}"
        r = requests.get(steam_api, timeout=10)
        data = r.json()
        if data[str(appid)]["success"]:
            game_data = data[str(appid)]["data"]
            game_name = game_data["name"]
            cover_img = game_data.get("header_image")
    except Exception as e:
        print(f"Gagal ambil data Steam: {e}")

    # Buat Embed
    embed = discord.Embed(
        title=f"✅ Manifest Generated: {game_name}",
        description=f"Successfully generated manifest files for **{game_name}** (`{appid}`)",
        color=0x2ecc71
    )
    embed.add_field(name="Links", value=f"[Steam Store]({steam_link}) | [SteamDB]({steamdb_link})", inline=False)

    try:
        # Kalau file belum ada → download dari GitHub
        if not os.path.exists(file_path):
            r = requests.get(zip_url, timeout=20)
            if r.status_code == 200:
                with open(file_path, "wb") as f:
                    f.write(r.content)
            else:
                embed.color = 0xe74c3c
                embed.add_field(
                    name="Manifest Status",
                    value=f"❌ Gagal download dari GitHub (HTTP {r.status_code})",
                    inline=False
                )
                await interaction.response.send_message(embed=embed)
                return

        # Kalau berhasil → attach file + gambar
        embed.add_field(name="Manifest Status", value="✅ Manifest downloaded successfully", inline=False)
        if cover_img:
            embed.set_image(url=cover_img)

        await interaction.response.send_message(embed=embed, file=discord.File(file_path))

        # Hapus file setelah dikirim (hemat storage)
        os.remove(file_path)

    except Exception as e:
        embed.color = 0xe74c3c
        embed.add_field(name="Manifest Status", value=f"❌ Error: {str(e)}", inline=False)
        await interaction.response.send_message(embed=embed)

# ====== RUN BOT ======
keep_alive()
bot.run(os.getenv("TOKEN"))  # Token disimpan di Replit Secrets
