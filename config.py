# config.py
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env (если он есть)
# Это удобно для локальной разработки. На сервере переменные лучше устанавливать напрямую.
load_dotenv()

# --- TELEGRAM API ---
# Получите их на my.telegram.org
API_ID = os.getenv("API_ID", "25474541")
API_HASH = os.getenv("API_HASH", "9eff5bd0104a3cc5ecfbeb8226ed7ecb")

# --- BOT TOKEN ---
# Получите у @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- ADMIN ---
# Ваш Telegram User ID
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))

# --- SUPPORT ---
# Контакт для кнопки "Техподдержка"
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "https://t.me/your_support_contact")

# --- DATABASE ---
DB_NAME = 'bot_group_spam_v3_access.db'

# --- SETTINGS ---
DEFAULT_DELAY_BETWEEN_COMMENTS = 20
MIN_DELAY_BETWEEN_COMMENTS = 10
MIN_DELAY_FOR_ATTACK = 0.5
NETWORK_TIMEOUT = 30  # Таймаут для сетевых операций в секундах

# --- PAGINATION ---
CHATS_PER_PAGE = 15
PROXIES_PER_PAGE = 15
SESSIONS_PER_PAGE = 10

# --- GEMINI ---
GEMINI_MODEL_NAME = "gemini-1.5-flash"
DEFAULT_GEMINI_PROMPT = "Перепиши следующий текст другими словами, сохранив его первоначальный смысл и примерную длину. Верни только измененный текст без каких-либо дополнительных комментариев или вступлений: "
