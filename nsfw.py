# Paste your full NSFW bot code here
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from nsfw_detector import predict
from PIL import Image
from pymongo import MongoClient
import os
import asyncio
from datetime import datetime, timedelta
from typing import Tuple
import numpy as np
from moviepy.editor import VideoFileClip

# ================== CONFIG ==================

API_ID = int(os.getenv("API_ID", "14050586"))
API_HASH = os.getenv("API_HASH", "42a60d9c657b106370c79bb0a8ac560c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8453519")

MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://K048@cluster0.4rfuzro.mongodb.nyWrites=true&w=majority")
mongo = MongoClient(MONGO_URL)
db = mongo["nsfw_bot"]
groups_col = db["groups"]   # per-group settings
warns_col = db["warns"]     # user warnings per group

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))  # -100... , 0 = disabled

DEFAULT_WARN_LIMIT = int(os.getenv("DEFAULT_WARN_LIMIT", "3"))
DEFAULT_SENSITIVITY = float(os.getenv("DEFAULT_SENSITIVITY", "0.70"))
DEFAULT_MUTE_SECONDS = int(os.getenv("DEFAULT_MUTE_SECONDS", "300"))  # 5 min

# Sticker ID for warnings (replace with your own sticker file_id)
WARNING_STICKER_ID = os.getenv("WARNING_STICKER_ID", "CAACAgEAAxkBAAEOAt1nzB8eR9Q5HAy3WsC9JWY3QFPFkAACpQQAAry3yUVbTjRNiKwMWTYE")

# ================== BOT INIT ==================

app = Client(
    "NSFW-MOD-BOT",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Load NSFW model globally
MODEL_PATH = os.getenv("MODEL_PATH", "mobilenet_v2_140_224.h5")
model = predict.load_model(MODEL_PATH)


# ================== HELPERS ==================

def get_group_settings(chat_id: int) -> dict:
    s = groups_col.find_one({"chat_id": chat_id}) or {}
    if not s:
        s = {
            "chat_id": chat_id,
            "filter_enabled": True,
            "action": "mute",  # or "ban"
            "warn_limit": DEFAULT_WARN_LIMIT,
            "sensitivity": DEFAULT_SENSITIVITY,
            "admin_bypass": True,
            "mute_seconds": DEFAULT_MUTE_SECONDS,
        }
        groups_col.insert_one(s)
    else:
        # make sure all keys exist
        s.setdefault("filter_enabled", True)
        s.setdefault("action", "mute")
        s.setdefault("warn_limit", DEFAULT_WARN_LIMIT)
        s.setdefault("sensitivity", DEFAULT_SENSITIVITY)
        s.setdefault("admin_bypass", True)
        s.setdefault("mute_seconds", DEFAULT_MUTE_SECONDS)
        groups_col.update_one({"chat_id": chat_id}, {"$set": s})
    return s


def update_group_settings(chat_id: int, settings: dict) -> dict:
    groups_col.update_one({"chat_id": chat_id}, {"$set": settings}, upsert=True)
    return get_group_settings(chat_id)


async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def detect_nsfw_image(path: str, sensitivity: float) -> Tuple[bool, float]:
    result = predict.classify(model, path)
    data = list(result.values())[0]
    nsfw_score = data.get("porn", 0) + data.get("sexy", 0) + data.get("hentai", 0)
    return nsfw_score >= sensitivity, nsfw_score


def sample_video_frames(video_path: str, num_frames: int = 5):
    clip = VideoFileClip(video_path)
    duration = clip.duration or 0
    if duration <= 0:
        clip.close()
        return []
    times = np.linspace(0, duration, num_frames + 2)[1:-1]
    frames = []
    for t in times:
        frame = clip.get_frame(float(t))
        frames.append(frame)
    clip.close()
    return frames


async def detect_nsfw_video(video_path: str, sensitivity: float) -> Tuple[bool, float]:
    """
    Video frames sample karega, har frame pe image detector chalega.
    Agar koi frame sensitivity cross kare to NSFW treat karega.
    """
    frames = sample_video_frames(video_path, num_frames=5)
    if not frames:
        return False, 0.0

    tmp_files = []
    import uuid
    from imageio import imwrite

    try:
        max_score = 0.0
        for frame in frames:
            tmp_path = f"/tmp/nsfw_frame_{uuid.uuid4().hex}.jpg"
            imwrite(tmp_path, frame)
            tmp_files.append(tmp_path)
            is_nsfw, score = await detect_nsfw_image(tmp_path, sensitivity)
            if score > max_score:
                max_score = score
            if is_nsfw:
                return True, score
        return False, max_score
    finally:
        for f in tmp_files:
            try:
                os.remove(f)
            except Exception:
                pass


async def add_warning(user_id: int, chat_id: int) -> int:
    """
    User ke warns+1 karega, updated warns return.
    """
    doc = warns_col.find_one({"user_id": user_id, "chat_id": chat_id}) or {
        "user_id": user_id,
        "chat_id": chat_id,
        "warns": 0,
    }
    doc["warns"] += 1
    warns_col.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": {"warns": doc["warns"]}},
        upsert=True,
    )
    return doc["warns"]


async def reset_warnings(user_id: int, chat_id: int):
    warns_col.delete_one({"user_id": user_id, "chat_id": chat_id})


async def take_action(client: Client, message: Message, settings: dict, reason: str):
    """
    Warn count check karega, sticker + message + log bhejega,
    warn_limit cross hone pe ban/mute karega.
    """
    chat_id = message.chat.id
    user = message.from_user
    if not user:
        return
    user_id = user.id

    warn_count = await add_warning(user_id, chat_id)
    warn_limit = settings["warn_limit"]

    # Sticker warning (optional)
    if WARNING_STICKER_ID:
        try:
            await client.send_sticker(chat_id, WARNING_STICKER_ID, reply_to_message_id=message.id)
        except Exception:
            pass

    # Group me warning text
    text = (
        f"⚠️ **NSFW Content Detected**\n"
        f"👤 {user.mention}\n"
        f"💬 Warns: `{warn_count}/{warn_limit}`\n"
        f"📌 Reason: {reason}"
    )
    await message.reply_text(text)

    # Log channel me report
    if LOG_CHANNEL_ID != 0:
        try:
            await client.send_message(
                LOG_CHANNEL_ID,
                f"🚨 NSFW in {message.chat.title} (`{chat_id}`)\n"
                f"👤 User: {user.mention} (`{user_id}`)\n"
                f"Warns: {warn_count}/{warn_limit}\n"
                f"Reason: {reason}",
            )
        except Exception:
            pass

    # Warn limit cross → action
    if warn_count >= warn_limit:
        action = settings.get("action", "mute")
        if action == "ban":
            try:
                await message.chat.ban_member(user_id)
                await message.reply_text(f"⛔ {user.mention} **banned for repeated NSFW content.**")
            except Exception as e:
                await message.reply_text(f"❌ Failed to ban user: `{e}`")
        else:  # mute
            try:
                until_date = int((datetime.utcnow() + timedelta(seconds=settings["mute_seconds"])).timestamp())
                await message.chat.restrict_member(
                    user_id,
                    permissions={"can_send_messages": False},
                    until_date=until_date,
                )
                await message.reply_text(
                    f"🤐 {user.mention} **muted for {settings['mute_seconds']} seconds due to NSFW.**"
                )
            except Exception as e:
                await message.reply_text(f"❌ Failed to mute user: `{e}`")

        await reset_warnings(user_id, chat_id)


# ================== INLINE SETTINGS ==================

def build_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    action = settings["action"]
    filter_status = "ON ✅" if settings["filter_enabled"] else "OFF ❌"
    bypass_status = "ON ✅" if settings["admin_bypass"] else "OFF ❌"

    buttons = [
        [
            InlineKeyboardButton(f"Filter: {filter_status}", callback_data="nsfwset:toggle_filter"),
            InlineKeyboardButton(f"Action: {action.upper()}", callback_data="nsfwset:toggle_action"),
        ],
        [
            InlineKeyboardButton(f"Admin Bypass: {bypass_status}", callback_data="nsfwset:toggle_bypass"),
        ],
        [
            InlineKeyboardButton(f"Sensitivity: {settings['sensitivity']:.2f}", callback_data="nsfwset:cycle_sens"),
        ],
        [
            InlineKeyboardButton(f"Warn Limit: {settings['warn_limit']}", callback_data="nsfwset:cycle_warn"),
        ],
        [
            InlineKeyboardButton(f"Mute: {settings['mute_seconds']}s", callback_data="nsfwset:cycle_mute"),
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="nsfwset:close"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def cycle_value(current, options):
    try:
        idx = options.index(current)
    except ValueError:
        return options[0]
    return options[(idx + 1) % len(options)]


@app.on_message(filters.command("settings") & filters.group)
async def group_settings_cmd(client: Client, message: Message):
    if not await is_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text("Only admins can open NSFW settings.")
    settings = get_group_settings(message.chat.id)
    text = (
        "⚙️ **NSFW Filter Settings**\n\n"
        f"Filter: `{'ON' if settings['filter_enabled'] else 'OFF'}`\n"
        f"Action: `{settings['action']}`\n"
        f"Warnings Limit: `{settings['warn_limit']}`\n"
        f"Sensitivity: `{settings['sensitivity']:.2f}`\n"
        f"Mute Duration: `{settings['mute_seconds']}s`\n"
        f"Admin Bypass: `{settings['admin_bypass']}`"
    )
    await message.reply_text(text, reply_markup=build_settings_keyboard(settings))


@app.on_callback_query(filters.regex(r"^nsfwset:"))
async def settings_callback(client: Client, query: CallbackQuery):
    if not await is_admin(client, query.message.chat.id, query.from_user.id):
        return await query.answer("Admins only.", show_alert=True)

    chat_id = query.message.chat.id
    data = query.data.split(":", 1)[1]
    settings = get_group_settings(chat_id)

    if data == "toggle_filter":
        settings["filter_enabled"] = not settings["filter_enabled"]
    elif data == "toggle_action":
        settings["action"] = "ban" if settings["action"] == "mute" else "mute"
    elif data == "toggle_bypass":
        settings["admin_bypass"] = not settings["admin_bypass"]
    elif data == "cycle_sens":
        options = [0.50, 0.60, 0.70, 0.80, 0.90]
        settings["sensitivity"] = cycle_value(settings["sensitivity"], options)
    elif data == "cycle_warn":
        options = [1, 2, 3, 5]
        settings["warn_limit"] = cycle_value(settings["warn_limit"], options)
    elif data == "cycle_mute":
        options = [60, 300, 900, 3600]
        settings["mute_seconds"] = cycle_value(settings["mute_seconds"], options)
    elif data == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return await query.answer("Closed.")
    else:
        return await query.answer("Unknown option.")

    update_group_settings(chat_id, settings)
    await query.message.edit_reply_markup(build_settings_keyboard(settings))
    await query.answer("Updated ✅")


# ================== NSFW PROCESSOR ==================

async def process_nsfw_message(client: Client, message: Message, media_type: str, file_path: str):
    chat_id = message.chat.id
    user = message.from_user
    if not user:
        return

    settings = get_group_settings(chat_id)

    if not settings["filter_enabled"]:
        return

    # Admin bypass
    if settings["admin_bypass"] and await is_admin(client, chat_id, user.id):
        return

    # Detect NSFW
    if media_type == "image":
        is_nsfw, score = await detect_nsfw_image(file_path, settings["sensitivity"])
    else:
        is_nsfw, score = await detect_nsfw_video(file_path, settings["sensitivity"])

    if not is_nsfw:
        return

    # Delete message
    try:
        await message.delete()
    except Exception:
        pass

    reason = f"{media_type.upper()} NSFW (score={score:.2f})"
    await take_action(client, message, settings, reason)


# ================== MEDIA HANDLERS ==================

@app.on_message(filters.photo & filters.group)
async def on_photo(client: Client, message: Message):
    photo_path = await message.download()
    try:
        await process_nsfw_message(client, message, "image", photo_path)
    finally:
        try:
            os.remove(photo_path)
        except Exception:
            pass


@app.on_message(filters.video & filters.group)
async def on_video(client: Client, message: Message):
    video_path = await message.download()
    try:
        await process_nsfw_message(client, message, "video", video_path)
    finally:
        try:
            os.remove(video_path)
        except Exception:
            pass


# ================== START / HELP ==================

def pm_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Add me to a Group", url="https://t.me/YourBotUsername?startgroup=true"),
            ],
            [
                InlineKeyboardButton("❓ Help", callback_data="pm:help"),
                InlineKeyboardButton("ℹ️ About", callback_data="pm:about"),
            ],
        ]
    )


@app.on_message(filters.private & filters.command("start"))
async def pm_start(client: Client, message: Message):
    text = (
        "👋 **NSFW Guard Bot**\n\n"
        "I can protect your groups from adult images & videos.\n\n"
        "➕ Add me to your group\n"
        "🛡 I auto-delete NSFW\n"
        "⚙️ Admins can control warnings, ban/mute, sensitivity, etc.\n"
    )
    await message.reply_text(text, reply_markup=pm_start_keyboard())


@app.on_callback_query(filters.regex(r"^pm:"))
async def pm_buttons(client: Client, query: CallbackQuery):
    data = query.data.split(":", 1)[1]
    if data == "help":
        text = (
            "❓ **Help - NSFW Guard Bot**\n\n"
            "• Add me as admin in your group.\n"
            "• I need **Delete Messages** and **Ban/Restrict Members** permissions.\n"
            "• I will auto-delete NSFW images/videos.\n"
            "• Use /settings in group (admin only) to configure.\n"
        )
        await query.message.edit_text(text, reply_markup=pm_start_keyboard())
    elif data == "about":
        text = (
            "ℹ️ **About NSFW Guard Bot**\n\n"
            "• Free NSFW moderation bot.\n"
            "• Uses a local ML model, no paid API.\n"
            "• Logs violations (optional) to a private channel.\n"
        )
        await query.message.edit_text(text, reply_markup=pm_start_keyboard())
    await query.answer()


@app.on_message(filters.group & filters.command("start"))
async def group_start(client: Client, message: Message):
    await message.reply_text(
        "👋 NSFW Guard active in this group.\n"
        "Admins can use /settings to configure filter."
    )


if __name__ == "__main__":
    app.run()