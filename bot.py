import asyncio
import logging
import os
import threading
from urllib.parse import quote

import aiohttp
from flask import Flask, render_template_string, request, jsonify
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# Configuration & Tokens
BOT_TOKEN = os.getenv("BOT_TOKEN", "8693567460:AAGCm7E5sZQe90MU6WP20G_e-R_n22DCjUY").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
PORT = int(os.getenv("PORT", 5000))

if not BOT_TOKEN or not GROQ_API_KEY:
    raise SystemExit("Set BOT_TOKEN and GROQ_API_KEY first.")

logging.basicConfig(level=logging.INFO)

# Telegram Bot Setup
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

histories = {}
languages = {}

SYSTEMS = {
    "en": "You are SABUJ AI, an elite AI developer assistant created and owned by Sabuj Hawlader. Reply in English.",
    "hi": "You are SABUJ AI, an elite AI developer assistant created and owned by Sabuj Hawlader. Reply in Hindi.",
    "ta": "You are SABUJ AI, an elite AI developer assistant created and owned by Sabuj Hawlader. Reply in Tamil.",
}

HELP_TEXT = """🤖 **SABUJ AI PANEL DASHBOARD**

Send any message to chat with AI directly.

⚡ **Interactive Menu & Commands:**
• `/start` - Open main dashboard
• `/help` - Show help manual
• `/image <prompt>` - Generate AI image instantly
• `/lang` - Switch bot language (EN / HI / TA)
• `/clear` - Clear chat history memory"""

def get_main_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎨 Generate Image", callback_data="menu_image_help"),
                InlineKeyboardButton(text="🌐 Language Menu", callback_data="menu_lang_panel")
            ],
            [
                InlineKeyboardButton(text="🧹 Clear Memory", callback_data="menu_clear"),
                InlineKeyboardButton(text="ℹ️ Help Guide", callback_data="menu_help")
            ]
        ]
    )

def get_language_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang_en"),
                InlineKeyboardButton(text="🇮🇳 Hindi", callback_data="set_lang_hi"),
                InlineKeyboardButton(text="🇮🇳 Tamil", callback_data="set_lang_ta")
            ],
            [
                InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_start")
            ]
        ]
    )

async def groq_ai_request(messages_list):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": messages_list,
                "temperature": 0.7,
                "max_tokens": 1200,
            },
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
            return data["choices"][0]["message"]["content"].strip()

# Telegram Handlers
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "✨ **Welcome to SABUJ AI PANEL!**\n\n"
        "💬 Powered by LLaMA 3.3 Engine & Web UI Studio\n"
        "Select an option from the control panel below:",
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(HELP_TEXT, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

@dp.message(Command("clear"))
async def clear_cmd(message: Message):
    histories.pop(message.from_user.id, None)
    await message.answer("🧹 Chat history memory successfully cleared!", reply_markup=get_main_menu_keyboard())

@dp.message(Command("lang"))
async def lang_cmd(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    lang = parts[1].strip().lower() if len(parts) > 1 else ""
    if lang not in SYSTEMS:
        await message.answer(
            "🌐 **Select Bot Language:**\nClick a button below to update language preference.",
            reply_markup=get_language_keyboard(),
            parse_mode="Markdown"
        )
        return
    languages[message.from_user.id] = lang
    await message.answer(f"✅ Language successfully updated to **{lang.upper()}**.", reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

@dp.message(Command("image"))
async def image_cmd(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "🎨 **Usage Guide:** `/image <description>`\nExample: `/image futuristic cyberpunk city`",
            parse_mode="Markdown"
        )
        return
    prompt = parts[1].strip()
    if len(prompt) > 800:
        await message.answer("⚠ Prompt must be under 800 characters.")
        return

    status = await message.answer("🎨 Generating your AI image...")

    try:
        await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
        url = "https://image.pollinations.ai/prompt/" + quote(prompt, safe="")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async with session.get(url, params={"width": "1024", "height": "1024", "nologo": "true"}) as response:
                if response.status != 200:
                    await status.edit_text("⚠️ Image generation service error.")
                    return
                image_data = await response.read()

        photo = BufferedInputFile(image_data, filename="sabuj_ai_image.jpg")
        await message.answer_photo(photo, caption=f"🎨 **Prompt:** {prompt[:900]}", parse_mode="Markdown")
        await status.delete()
    except Exception:
        logging.exception("Image generation error")
        await status.edit_text("⚠️ Could not generate image right now.")

@dp.callback_query(F.data.startswith("menu_") | F.data.startswith("set_lang_"))
async def callback_handler(callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id

    if data == "menu_start":
        await callback.message.edit_text(
            "✨ **Welcome to SABUJ AI PANEL!**\n\nSelect an option from the control panel below:",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown"
        )
    elif data == "menu_help":
        await callback.message.edit_text(HELP_TEXT, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
    elif data == "menu_clear":
        histories.pop(user_id, None)
        await callback.answer("🧹 History cleared!", show_alert=True)
    elif data == "menu_image_help":
        await callback.message.edit_text(
            "🎨 **Image Generation Guide:**\n\nType `/image <your prompt>` in chat to generate instant high-quality images.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown"
        )
    elif data == "menu_lang_panel":
        await callback.message.edit_text(
            "🌐 **Choose your preferred language:**",
            reply_markup=get_language_keyboard(),
            parse_mode="Markdown"
        )
    elif data.startswith("set_lang_"):
        lang = data.split("_")[2]
        languages[user_id] = lang
        await callback.answer(f"Language set to {lang.upper()}!", show_alert=True)
        await callback.message.edit_text(
            f"✅ Language successfully updated to **{lang.upper()}**.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.message(F.text)
async def chat_cmd(message: Message):
    prompt = (message.text or "").strip()
    if not prompt:
        return
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        user_id = message.from_user.id
        lang = languages.get(user_id, "en")
        history = histories.setdefault(user_id, [])

        messages = [{"role": "system", "content": SYSTEMS.get(lang, SYSTEMS["en"])}]
        messages += history[-12:]
        messages.append({"role": "user", "content": prompt})

        answer = await groq_ai_request(messages)
        if not answer:
            answer = "⚠️ AI service error."

        history.extend([
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": answer},
        ])
        histories[user_id] = history[-12:]

        for i in range(0, len(answer), 4000):
            await message.answer(answer[i:i + 4000])
    except Exception:
        logging.exception("Chat response error")
        await message.answer("⚠️ Check your internet connection or API key.")


# Flask Web UI Application (ChatGPT / Claude Style UI)
app = Flask(__name__)

WEB_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SABUJ AI - Web Studio & Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gray-900 text-gray-100 h-screen flex flex-col">
    <!-- Header -->
    <header class="bg-gray-800 border-b border-gray-700 p-4 flex justify-between items-center shadow-md">
        <div class="flex items-center space-x-3">
            <div class="bg-indigo-600 text-white p-2 rounded-lg font-bold"><i class="fa-solid fa-brain"></i></div>
            <h1 class="text-xl font-bold tracking-wide">SABUJ AI <span class="text-xs bg-indigo-500 text-white px-2 py-0.5 rounded-full">v3.3 Studio</span></h1>
        </div>
        <div class="text-sm text-gray-400">Owner: <span class="text-indigo-400 font-semibold">Sabuj Hawlader</span></div>
    </header>

    <!-- Chat Container -->
    <main id="chat-container" class="flex-1 overflow-y-auto p-4 space-y-6 max-w-4xl w-full mx-auto">
        <div class="flex items-start space-x-4">
            <div class="bg-indigo-600 text-white rounded-full h-10 w-10 flex items-center justify-center font-bold flex-shrink-0">AI</div>
            <div class="bg-gray-800 p-4 rounded-2xl shadow-lg border border-gray-700 max-w-xl">
                <p class="font-medium">Hello! I am **SABUJ AI**, your custom web assistant created by Sabuj Hawlader. How can I help you today with your code, design, or project?</p>
            </div>
        </div>
    </main>

    <!-- Input Box -->
    <footer class="bg-gray-800 border-t border-gray-700 p-4 shadow-lg">
        <form id="chat-form" class="max-w-4xl mx-auto flex items-center space-x-3">
            <input type="text" id="user-input" placeholder="Type your message or prompt here..." autocomplete="off"
                class="flex-1 bg-gray-900 border border-gray-700 rounded-xl px-4 py-3 focus:outline-none focus:border-indigo-500 text-gray-100 placeholder-gray-500">
            <button type="submit" class="bg-indigo-600 hover:bg-indigo-500 text-white px-6 py-3 rounded-xl font-semibold transition flex items-center space-x-2">
                <span>Send</span> <i class="fa-solid fa-paper-plane"></i>
            </button>
        </form>
    </footer>

    <script>
        const chatContainer = document.getElementById('chat-container');
        const chatForm = document.getElementById('chat-form');
        const userInput = document.getElementById('user-input');

        chatForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const text = userInput.value.trim();
            if (!text) return;

            // Append User Message
            chatContainer.innerHTML += `
                <div class="flex items-start justify-end space-x-4">
                    <div class="bg-gray-700 p-4 rounded-2xl shadow-lg border border-gray-600 max-w-xl text-gray-100">
                        <p>${escapeHtml(text)}</p>
                    </div>
                    <div class="bg-gray-600 text-white rounded-full h-10 w-10 flex items-center justify-center font-bold flex-shrink-0">You</div>
                </div>`;
            userInput.value = '';
            chatContainer.scrollTop = chatContainer.scrollHeight;

            // Append Loading Indicator
            const loadId = 'loading-' + Date.now();
            chatContainer.innerHTML += `
                <div id="${loadId}" class="flex items-start space-x-4">
                    <div class="bg-indigo-600 text-white rounded-full h-10 w-10 flex items-center justify-center font-bold flex-shrink-0">AI</div>
                    <div class="bg-gray-800 p-4 rounded-2xl shadow-lg border border-gray-700 text-gray-400 italic">Thinking...</div>
                </div>`;
            chatContainer.scrollTop = chatContainer.scrollHeight;

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: text })
                });
                const data = await response.json();
                document.getElementById(loadId).remove();

                chatContainer.innerHTML += `
                    <div class="flex items-start space-x-4">
                        <div class="bg-indigo-600 text-white rounded-full h-10 w-10 flex items-center justify-center font-bold flex-shrink-0">AI</div>
                        <div class="bg-gray-800 p-4 rounded-2xl shadow-lg border border-gray-700 max-w-xl text-gray-100">
                            <p class="whitespace-pre-wrap">${escapeHtml(data.reply || "No response")}</p>
                        </div>
                    </div>`;
            } catch (err) {
                document.getElementById(loadId).remove();
                chatContainer.innerHTML += `<div class="text-red-400 text-center">Error connecting to server.</div>`;
            }
            chatContainer.scrollTop = chatContainer.scrollHeight;
        });

        function escapeHtml(text) {
            return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(WEB_HTML)

@app.route("/api/chat", methods=["POST"])
def web_chat():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"reply": "Please provide a valid prompt."})
    
    # Run synchronous request to Groq for Web UI
    import requests
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEMS["en"]},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 1200
    }
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=30)
        res_data = res.json()
        reply = res_data["choices"][0]["message"]["content"].strip()
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"Error: {str(e)}"})


def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

async def main():
    # Start Flask Web UI in a separate background daemon thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logging.info(f"Web UI Studio started on port {PORT}")

    # Start Telegram Bot Polling
    logging.info("Starting Telegram Bot Polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
