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
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo

# Configuration & Tokens
BOT_TOKEN = os.getenv("BOT_TOKEN", "8693567460:AAGCm7E5sZQe90MU6WP20G_e-R_n22DCjUY").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
PORT = int(os.getenv("PORT", 5000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise SystemExit("Set BOT_TOKEN and GROQ_API_KEY first.")

logging.basicConfig(level=logging.INFO)

# Telegram Bot Setup
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

histories = {}
languages = {}
user_modes = {}  # Tracks if user is in 'chat', 'code', or 'image' mode

SYSTEMS = {
    "en": "You are SABUJ AI, an elite professional AI developer assistant and full-stack software engineer created and owned by Sabuj Hawlader. Provide clean, structured, and professional code blocks or explanations.",
    "hi": "You are SABUJ AI, an elite professional AI developer assistant created and owned by Sabuj Hawlader. Reply in Hindi.",
    "ta": "You are SABUJ AI, an elite professional AI developer assistant created and owned by Sabuj Hawlader. Reply in Tamil.",
}

HELP_TEXT = """🤖 **SABUJ AI PROFESSIONAL PANEL**

⚡ **Modes & Features:**
• **Normal Chat Mode:** General conversation & expert assistance.
• **Code Engineer Mode:** Professional syntax and program structures.
• **Instant Image Mode:** Generate stunning AI images without commands.

🛠 **Commands:**
• `/start` - Open Main Dashboard & Controls
• `/help` - Show Help Manual
• `/clear` - Wipe chat session memory
• `/lang` - Change language preference"""

def get_main_menu_keyboard(webapp_url):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Chat Mode", callback_data="mode_chat"),
                InlineKeyboardButton(text="💻 Code Engineer", callback_data="mode_code")
            ],
            [
                InlineKeyboardButton(text="🎨 Instant Image Gen", callback_data="mode_image"),
                InlineKeyboardButton(text="🌐 Open Web UI Studio", web_app=WebAppInfo(url=webapp_url))
            ],
            [
                InlineKeyboardButton(text="🌐 Language Menu", callback_data="menu_lang_panel"),
                InlineKeyboardButton(text="🧹 Clear Memory", callback_data="menu_clear")
            ],
            [
                InlineKeyboardButton(text="ℹ️ Help & Manual", callback_data="menu_help")
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
                "max_tokens": 1500,
            },
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
            return data["choices"][0]["message"]["content"].strip()

# Telegram Handlers
@dp.message(CommandStart())
async def start(message: Message):
    user_modes[message.from_user.id] = "chat"
    await message.answer(
        "✨ **Welcome to SABUJ AI Studio & Dashboard!**\n\n"
        "Engineered by **Sabuj Hawlader**. Select your operational mode or launch the Web UI inside Telegram:",
        reply_markup=get_main_menu_keyboard(RENDER_EXTERNAL_URL),
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(HELP_TEXT, reply_markup=get_main_menu_keyboard(RENDER_EXTERNAL_URL), parse_mode="Markdown")

@dp.message(Command("clear"))
async def clear_cmd(message: Message):
    histories.pop(message.from_user.id, None)
    await message.answer("🧹 Memory cleared successfully!", reply_markup=get_main_menu_keyboard(RENDER_EXTERNAL_URL))

@dp.message(Command("lang"))
async def lang_cmd(message: Message):
    await message.answer(
        "🌐 **Select Language Preference:**",
        reply_markup=get_language_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("mode_") | F.data.startswith("menu_") | F.data.startswith("set_lang_"))
async def callback_handler(callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id

    if data == "mode_chat":
        user_modes[user_id] = "chat"
        await callback.answer("Switched to Normal Chat Mode!", show_alert=True)
    elif data == "mode_code":
        user_modes[user_id] = "code"
        await callback.answer("Switched to Code Engineer Mode!", show_alert=True)
    elif data == "mode_image":
        user_modes[user_id] = "image"
        await callback.answer("Switched to Instant Image Generation Mode! Send any description now.", show_alert=True)
    elif data == "menu_start":
        await callback.message.edit_text(
            "✨ **SABUJ AI Control Panel**\n\nSelect a mode or option below:",
            reply_markup=get_main_menu_keyboard(RENDER_EXTERNAL_URL),
            parse_mode="Markdown"
        )
    elif data == "menu_help":
        await callback.message.edit_text(HELP_TEXT, reply_markup=get_main_menu_keyboard(RENDER_EXTERNAL_URL), parse_mode="Markdown")
    elif data == "menu_clear":
        histories.pop(user_id, None)
        await callback.answer("🧹 History cleared!", show_alert=True)
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
            reply_markup=get_main_menu_keyboard(RENDER_EXTERNAL_URL),
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.message(F.text)
async def message_router(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return

    mode = user_modes.get(user_id, "chat")

    # If user selected Image Generation mode via buttons
    if mode == "image":
        status = await message.answer("🎨 Generating your AI image...")
        try:
            await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
            url = "https://image.pollinations.ai/prompt/" + quote(text, safe="")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
                async with session.get(url, params={"width": "1024", "height": "1024", "nologo": "true"}) as response:
                    if response.status != 200:
                        await status.edit_text("⚠️ Image generation service error.")
                        return
                    image_data = await response.read()

            photo = BufferedInputFile(image_data, filename="sabuj_ai_image.jpg")
            
            # Action button under image preview
            preview_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🌐 Open Web UI Studio", web_app=WebAppInfo(url=RENDER_EXTERNAL_URL))],
                    [InlineKeyboardButton(text="🔙 Back to Menu", callback_data="menu_start")]
                ]
            )
            await message.answer_photo(photo, caption=f"🎨 **Prompt:** {text[:900]}", reply_markup=preview_kb, parse_mode="Markdown")
            await status.delete()
        except Exception:
            logging.exception("Image generation error")
            await status.edit_text("⚠️ Could not generate image right now.")
        return

    # Chat & Code Engineer Modes
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        lang = languages.get(user_id, "en")
        history = histories.setdefault(user_id, [])

        system_prompt = SYSTEMS.get(lang, SYSTEMS["en"])
        if mode == "code":
            system_prompt += " Provide professional, clean production-ready code with complete explanations."

        messages = [{"role": "system", "content": system_prompt}]
        messages += history[-12:]
        messages.append({"role": "user", "content": text})

        answer = await groq_ai_request(messages)
        if not answer:
            answer = "⚠️ AI service error."

        history.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ])
        histories[user_id] = history[-12:]

        # Response keyboard with Web App Chrome Preview button
        response_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Preview in Telegram Browser", web_app=WebAppInfo(url=RENDER_EXTERNAL_URL))],
                [InlineKeyboardButton(text="🎛 Control Panel", callback_data="menu_start")]
            ]
        )

        for i in range(0, len(answer), 4000):
            chunk = answer[i:i + 4000]
            if i + 4000 >= len(answer):
                await message.answer(chunk, reply_markup=response_kb, parse_mode="Markdown")
            else:
                await message.answer(chunk, parse_mode="Markdown")

    except Exception:
        logging.exception("Chat response error")
        await message.answer("⚠️ Check your internet connection or API key.")


# Flask Web UI Application (ChatGPT / Claude Style UI with Sidebar & Mobile Support)
app = Flask(__name__)

WEB_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SABUJ AI - Professional Studio & Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gray-950 text-gray-100 h-screen flex overflow-hidden">
    
    <!-- Sidebar for Desktop & Mobile Toggle -->
    <aside id="sidebar" class="bg-gray-900 border-r border-gray-800 w-64 flex flex-col justify-between p-4 transition-all duration-300 z-20 absolute md:relative -translate-x-full md:translate-x-0 h-full">
        <div>
            <div class="flex items-center justify-between mb-6">
                <div class="flex items-center space-x-3">
                    <div class="bg-indigo-600 text-white p-2 rounded-xl font-bold"><i class="fa-solid fa-brain"></i></div>
                    <h1 class="text-lg font-bold tracking-wide">SABUJ AI</h1>
                </div>
                <button id="close-sidebar" class="md:hidden text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button>
            </div>
            <button onclick="clearChat()" class="w-full bg-indigo-600/20 hover:bg-indigo-600/30 border border-indigo-500/30 text-indigo-300 py-2.5 px-4 rounded-xl font-medium transition flex items-center justify-center space-x-2 mb-4">
                <i class="fa-solid fa-plus"></i> <span>New Chat Session</span>
            </button>
            <div class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Modes</div>
            <div class="space-y-1">
                <button class="w-full text-left px-3 py-2 rounded-lg bg-gray-800 text-indigo-400 font-medium text-sm flex items-center space-x-3"><i class="fa-solid fa-comments"></i><span>Chat Assistant</span></button>
                <button class="w-full text-left px-3 py-2 rounded-lg hover:bg-gray-800/60 text-gray-400 hover:text-gray-200 text-sm flex items-center space-x-3"><i class="fa-solid fa-code"></i><span>Code Engineer</span></button>
            </div>
        </div>
        <div class="border-t border-gray-800 pt-4 text-xs text-gray-400">
            Created by <span class="text-indigo-400 font-semibold">Sabuj Hawlader</span>
        </div>
    </aside>

    <!-- Main Chat Area -->
    <div class="flex-1 flex flex-col h-full bg-gray-950">
        <!-- Header -->
        <header class="bg-gray-900/50 backdrop-blur border-b border-gray-800 p-4 flex justify-between items-center">
            <div class="flex items-center space-x-3">
                <button id="sidebar-toggle" class="text-gray-300 hover:text-white md:hidden"><i class="fa-solid fa-bars text-xl"></i></button>
                <h2 class="font-semibold text-gray-200">SABUJ AI Studio v3.3</h2>
            </div>
            <span class="text-xs bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 px-3 py-1 rounded-full font-medium">Online Engine</span>
        </header>

        <!-- Messages Container -->
        <main id="chat-container" class="flex-1 overflow-y-auto p-4 md:p-6 space-y-6 max-w-4xl w-full mx-auto">
            <div class="flex items-start space-x-4">
                <div class="bg-indigo-600 text-white rounded-2xl h-10 w-10 flex items-center justify-center font-bold flex-shrink-0 shadow-lg shadow-indigo-600/30">AI</div>
                <div class="bg-gray-900 border border-gray-800 p-4 rounded-2xl shadow-xl max-w-xl text-gray-200">
                    <p class="font-medium">Hello Sabuj! I am your fully upgraded AI assistant and software engineering studio. How can I assist you today?</p>
                </div>
            </div>
        </main>

        <!-- Input Footer -->
        <footer class="p-4 bg-gray-950 border-t border-gray-800">
            <form id="chat-form" class="max-w-4xl mx-auto flex items-center space-x-3 bg-gray-900 border border-gray-800 rounded-2xl p-2 shadow-2xl">
                <input type="text" id="user-input" placeholder="Ask anything or request code..." autocomplete="off"
                    class="flex-1 bg-transparent border-none px-4 py-2 focus:outline-none text-gray-100 placeholder-gray-500 text-sm md:text-base">
                <button type="submit" class="bg-indigo-600 hover:bg-indigo-500 text-white px-5 py-2.5 rounded-xl font-semibold transition flex items-center space-x-2 shadow-lg shadow-indigo-600/30">
                    <span>Send</span> <i class="fa-solid fa-paper-plane text-xs"></i>
                </button>
            </form>
        </footer>
    </div>

    <script>
        const sidebar = document.getElementById('sidebar');
        const sidebarToggle = document.getElementById('sidebar-toggle');
        const closeSidebar = document.getElementById('close-sidebar');
        const chatContainer = document.getElementById('chat-container');
        const chatForm = document.getElementById('chat-form');
        const userInput = document.getElementById('user-input');

        sidebarToggle.addEventListener('click', () => sidebar.classList.toggle('-translate-x-full'));
        closeSidebar.addEventListener('click', () => sidebar.classList.add('-translate-x-full'));

        function clearChat() {
            chatContainer.innerHTML = `
                <div class="flex items-start space-x-4">
                    <div class="bg-indigo-600 text-white rounded-2xl h-10 w-10 flex items-center justify-center font-bold flex-shrink-0 shadow-lg shadow-indigo-600/30">AI</div>
                    <div class="bg-gray-900 border border-gray-800 p-4 rounded-2xl shadow-xl max-w-xl text-gray-200">
                        <p class="font-medium">Session cleared. Ready for your next prompt!</p>
                    </div>
                </div>`;
        }

        chatForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const text = userInput.value.trim();
            if (!text) return;

            chatContainer.innerHTML += `
                <div class="flex items-start justify-end space-x-4">
                    <div class="bg-indigo-600/20 border border-indigo-500/30 p-4 rounded-2xl shadow-lg max-w-xl text-gray-100">
                        <p>${escapeHtml(text)}</p>
                    </div>
                    <div class="bg-gray-800 text-white rounded-2xl h-10 w-10 flex items-center justify-center font-bold flex-shrink-0">You</div>
                </div>`;
            userInput.value = '';
            chatContainer.scrollTop = chatContainer.scrollHeight;

            const loadId = 'loading-' + Date.now();
            chatContainer.innerHTML += `
                <div id="${loadId}" class="flex items-start space-x-4">
                    <div class="bg-indigo-600 text-white rounded-2xl h-10 w-10 flex items-center justify-center font-bold flex-shrink-0 shadow-lg shadow-indigo-600/30">AI</div>
                    <div class="bg-gray-900 border border-gray-800 p-4 rounded-2xl shadow-xl text-gray-400 italic">Thinking and generating code...</div>
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
                        <div class="bg-indigo-600 text-white rounded-2xl h-10 w-10 flex items-center justify-center font-bold flex-shrink-0 shadow-lg shadow-indigo-600/30">AI</div>
                        <div class="bg-gray-900 border border-gray-800 p-4 rounded-2xl shadow-xl max-w-xl text-gray-100">
                            <p class="whitespace-pre-wrap">${escapeHtml(data.reply || "No response")}</p>
                        </div>
                    </div>`;
            } catch (err) {
                document.getElementById(loadId).remove();
                chatContainer.innerHTML += `<div class="text-red-400 text-center text-sm">Server connection error.</div>`;
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
    
    import requests
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEMS["en"]},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 1500
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
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logging.info(f"Web UI Studio started on port {PORT}")

    logging.info("Starting Telegram Bot Polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
