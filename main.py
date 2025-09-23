import discord
from discord import app_commands
from discord.ext import commands
import os
import json
import time

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Simpan config di file biar persist
CONFIG_FILE = "config.json"
config = {}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    else:
        config = {"upload_channel": None, "update_channel": None, "request_channel": None, "mention_role": None}
        save_config()

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

# --- SETUP COMMANDS ---

@tree.command(name="channeluploadsetup", description="Set channel untuk notif upload")
@app_commands.describe(channel="Pilih channel upload")
async def channel_upload_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    config["upload_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ Channel upload diset ke {channel.mention}", ephemeral=True)

@tree.command(name="channelupdatesetup", description="Set channel untuk notif update patch")
@app_commands.describe(channel="Pilih channel update patch")
async def channel_update_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    config["update_channel"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ Channel update patch diset ke {channel.mention}", ephemeral=True)

@tree.command(name="channelrequestsetup", description="Set channel untuk notif request not found + role mention")
@app_commands.describe(channel="Pilih channel request not found", role="Role untuk mention jika request not found")
async def channel_request_setup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
    config["request_channel"] = channel.id
    config["mention_role"] = role.id if role else None
    save_config()
    mention_text = f" dan mention role {role.mention}" if role else ""
    await interaction.response.send_message(f"✅ Channel request not found diset ke {channel.mention}{mention_text}", ephemeral=True)

# --- GEN COMMAND (contoh dummy) ---

@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
@app_commands.describe(appid="Masukkan AppID")
async def gen(interaction: discord.Interaction, appid: str):
    start_time = time.time()

    # Dummy cek database
    found = appid not in ["11111", "250", "3489700"]  # anggap appid ini not found
    elapsed = round(time.time() - start_time, 2)

    if found:
        embed = discord.Embed(
            title="✅ Manifest Retrieved",
            color=discord.Color.green()
        )
        embed.add_field(name="🎮 Game", value=f"Dummy Game {appid}", inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value="1234 KB", inline=True)
        embed.add_field(name="📅 Release Date", value="2024-01-01", inline=True)
        embed.add_field(name="⏱️ Time", value=f"{elapsed}s", inline=True)
        embed.add_field(name="🙋 Requester", value=interaction.user.mention, inline=True)

        embed.add_field(name="🔗 Links", value=f"[Steam](https://store.steampowered.com/app/{appid}) | [SteamDB](https://steamdb.info/app/{appid}/)", inline=False)
        embed.add_field(name="📥 Download", value="File hanya bisa diunduh oleh requester (lihat bawah).", inline=False)
        embed.add_field(name="ℹ️ Info", value="Deskripsi game akan ditampilkan di sini.", inline=False)
        embed.set_image(url="https://cdn.cloudflare.steamstatic.com/steam/apps/1091500/header.jpg")

        await interaction.response.send_message(embed=embed)

    else:
        embed = discord.Embed(
            title="❌ Game Requested (Not Found)",
            color=discord.Color.red()
        )
        embed.add_field(name="🙋 User", value=interaction.user.mention, inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="🔗 Steam Store", value=f"[Open](https://store.steampowered.com/app/{appid})", inline=True)
        embed.add_field(name="🔗 SteamDB", value=f"[Open](https://steamdb.info/app/{appid}/)", inline=True)
        embed.set_footer(text="Requested via /gen")
        embed.set_thumbnail(url="https://cdn.cloudflare.steamstatic.com/steam/apps/1091500/header.jpg")

        # Kirim ke channel request not found + mention role kalau ada
        if config.get("request_channel"):
            channel = bot.get_channel(config["request_channel"])
            mention_role = f"<@&{config['mention_role']}>" if config.get("mention_role") else ""
            await channel.send(content=mention_role, embed=embed)

        await interaction.response.send_message(embed=embed)

# --- SYNC COMMANDS ---

@bot.event
async def on_ready():
    load_config()
    try:
        for guild in bot.guilds:
            synced = await tree.sync(guild=discord.Object(id=guild.id))
            print(f"🔄 Synced {len(synced)} commands ke guild {guild.name} ({guild.id})")
        synced_global = await tree.sync()
        print(f"🌍 Synced {len(synced_global)} commands global")
    except Exception as e:
        print("❌ Sync error:", e)

    print(f"✅ Bot logged in as {bot.user} — in {len(bot.guilds)} guilds")

# --- JALANKAN BOT ---

bot.run(os.getenv("DISCORD_TOKEN"))
