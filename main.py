import os
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread

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
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Bot {bot.user} sudah online dengan slash command!")

# === Cek Environment Variable ===
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not DISCORD_TOKEN:
    raise ValueError("❌ DISCORD_TOKEN tidak ditemukan! Pastikan sudah diset di Environment Variables Render.")
if not DISCORD_CHANNEL_ID:
    raise ValueError("❌ DISCORD_CHANNEL_ID tidak ditemukan! Pakai Channel ID, bukan Server ID.")

# === Contoh Command /ping ===
@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong! Bot aktif.")

# === Start ===
keep_alive()
bot.run(DISCORD_TOKEN)
