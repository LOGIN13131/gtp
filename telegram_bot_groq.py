import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import requests
from collections import defaultdict
import speech_recognition as sr
from pydub import AudioSegment
from gtts import gTTS
import os
import tempfile
import base64
from PIL import Image
import io
import json

# Настройка пути к FFmpeg
ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg-8.0.1-essentials_build", "bin")
os.environ["PATH"] += os.pathsep + ffmpeg_path
AudioSegment.converter = os.path.join(ffmpeg_path, "ffmpeg.exe")
AudioSegment.ffprobe = os.path.join(ffmpeg_path, "ffprobe.exe")

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Файл базы данных
DB_FILE = "bot_database.json"

def load_database():
    """Загрузка базы данных из файла"""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                logger.info(f"База данных загружена: {len(data.get('users', {}))} пользователей")
                return data
        except Exception as e:
            logger.error(f"Ошибка загрузки базы данных: {e}")
            return {"users": {}}
    return {"users": {}}

def save_database(data):
    """Сохранение базы данных в файл"""
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.info("База данных сохранена")
    except Exception as e:
        logger.error(f"Ошибка сохранения базы данных: {e}")

# Загружаем базу данных при старте
database = load_database()

# Токены
BOT_TOKEN = "8650080249:AAEAWeN-fujRQ5Ejh6tFpWJg_mnezqq7o44"
GROQ_API_KEY = "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"
OPENROUTER_API_KEY = "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"
GOOGLE_API_KEY = "AIzaSyDC666qST_7-o2xHsE4KE-KwohwGESFNa0"

# Админ ID
ADMIN_ID = 7359364482

# Память диалогов (для каждого пользователя)
user_conversations = defaultdict(list)
MAX_HISTORY = 20  # Последние 20 сообщений (было 10)

# Лимиты на день
from datetime import datetime, timedelta
import asyncio

def get_user_data(user_id):
    """Получить данные пользователя из базы"""
    user_id_str = str(user_id)
    if user_id_str not in database["users"]:
        database["users"][user_id_str] = {
            "limits": {
                "messages": {"count": 0, "reset_time": (datetime.now() + timedelta(hours=12)).isoformat()},
                "photos": {"count": 0, "reset_time": (datetime.now() + timedelta(hours=12)).isoformat()},
                "voice": {"count": 0, "reset_time": (datetime.now() + timedelta(hours=12)).isoformat()}
            },
            "stats": {"messages": 0, "voice": 0, "photos": 0, "images_generated": 0},
            "voice_enabled": False,
            "last_message": datetime.min.isoformat(),
            "conversations": [],
            "privilege_level": 0,
            "custom_cooldown": None,
            "banned": False,
            "first_seen": datetime.now().isoformat(),
            "username": None  # Username пользователя
        }
        save_database(database)
    return database["users"][user_id_str]

def update_username(user_id, username):
    """Обновить username пользователя"""
    user_data = get_user_data(user_id)
    user_data["username"] = username
    update_user_data(user_id, user_data)

def find_user_by_username(username):
    """Найти пользователя по username"""
    # Убираем @ если есть
    username = username.lstrip('@').lower()
    
    for user_id, data in database["users"].items():
        if data.get("username", "").lower() == username:
            return int(user_id)
    return None

def parse_user_identifier(identifier):
    """Распознать ID или username"""
    identifier = identifier.strip()
    
    # Если начинается с @, ищем по username
    if identifier.startswith('@'):
        user_id = find_user_by_username(identifier)
        if user_id:
            return user_id
        return None
    
    # Иначе пробуем как ID
    try:
        return int(identifier)
    except ValueError:
        # Может быть username без @
        user_id = find_user_by_username(identifier)
        return user_id

def update_user_data(user_id, data):
    """Обновить данные пользователя в базе"""
    user_id_str = str(user_id)
    database["users"][user_id_str] = data
    save_database(database)

# Память диалогов (для каждого пользователя)
user_conversations = defaultdict(list)
MAX_HISTORY = 20

# Затримка між повідомленнями
MESSAGE_COOLDOWN = 25  # секунд

DAILY_LIMITS = {
    "messages": 20,
    "photos": 5,
    "voice": 5
}

def check_and_update_limit(user_id, limit_type):
    """Проверка и обновление лимитов"""
    now = datetime.now()
    user_data = get_user_data(user_id)
    
    # Сброс лимита если прошло 12 часов для этого типа
    reset_time = datetime.fromisoformat(user_data["limits"][limit_type]["reset_time"])
    if now >= reset_time:
        user_data["limits"][limit_type] = {
            "count": 0,
            "reset_time": (now + timedelta(hours=12)).isoformat()
        }
    
    # Проверка лимита
    if user_data["limits"][limit_type]["count"] >= DAILY_LIMITS[limit_type]:
        return False
    
    # Увеличиваем счетчик
    user_data["limits"][limit_type]["count"] += 1
    user_data["stats"][limit_type] += 1
    update_user_data(user_id, user_data)
    return True

def get_remaining_limits(user_id):
    """Получить оставшиеся лимиты"""
    now = datetime.now()
    user_data = get_user_data(user_id)
    result = {}
    
    for limit_type in ["messages", "photos", "voice"]:
        reset_time = datetime.fromisoformat(user_data["limits"][limit_type]["reset_time"])
        if now >= reset_time:
            result[limit_type] = {
                "remaining": DAILY_LIMITS[limit_type],
                "time_left": timedelta(hours=12)
            }
        else:
            result[limit_type] = {
                "remaining": DAILY_LIMITS[limit_type] - user_data["limits"][limit_type]["count"],
                "time_left": reset_time - now
            }
    
    return result

def format_time_remaining(td):
    """Форматування часу що залишився"""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    
    if hours > 0:
        return f"{hours}г {minutes}хв"
    elif minutes > 0:
        return f"{minutes}хв"
    else:
        return "менше хвилини"

def check_cooldown(user_id):
    """Проверка затримки між повідомленнями"""
    user_data = get_user_data(user_id)
    
    # Привилегии 9 = без задержки
    if user_data.get("privilege_level", 0) == 9:
        user_data["last_message"] = datetime.now().isoformat()
        update_user_data(user_id, user_data)
        return True, 0
    
    now = datetime.now()
    last_time = datetime.fromisoformat(user_data["last_message"])
    
    # Используем кастомную задержку если установлена
    cooldown = user_data.get("custom_cooldown") or MESSAGE_COOLDOWN
    time_passed = (now - last_time).total_seconds()
    
    if time_passed < cooldown:
        remaining = int(cooldown - time_passed)
        return False, remaining
    
    user_data["last_message"] = now.isoformat()
    update_user_data(user_id, user_data)
    return True, 0

def get_admin_keyboard():
    """Клавіатура для адміна"""
    keyboard = [
        [KeyboardButton("📊 Статистика бота"), KeyboardButton("👥 Список користувачів")],
        [KeyboardButton("🔍 Інфо про юзера"), KeyboardButton("⭐ Видати привілеї")],
        [KeyboardButton("🚫 Забанити"), KeyboardButton("✅ Розбанити")],
        [KeyboardButton("⏱️ Змінити затримку"), KeyboardButton("➕ Додати ліміти")],
        [KeyboardButton("🔙 Звичайна клавіатура")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_keyboard():
    """Получить постоянную клавиатуру с кнопками"""
    keyboard = [
        [KeyboardButton("🎨 Намалювати"), KeyboardButton("🎤 Голос вкл/викл")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("🗑️ Очистити")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start с постоянными кнопками"""
    user_id = update.effective_user.id
    user_conversations[user_id].clear()
    
    # Обновляем username
    if update.effective_user.username:
        update_username(user_id, update.effective_user.username)
    
    # Инициализируем пользователя в базе
    get_user_data(user_id)
    
    welcome_text = (
        "Привіт! 👋 Я твій дружелюбний бот-помічник! 🤖✨\n\n"
        "Що я вмію:\n"
        "• 💬 Спілкуватися українською та російською\n"
        "• 🎤 Розпізнавати голосові повідомлення\n"
        "• 📷 Описувати фото детально\n"
        "• 🎨 Малювати картинки за описом\n"
        "• 🧠 Пам'ятати контекст розмови\n\n"
        "Використовуйте кнопки внизу або просто пишіть мені! 😊\n"
        "Я завжди радий допомогти! ❤️"
    )
    
    # Для админа добавляем информацию о командах
    if user_id == ADMIN_ID:
        welcome_text += "\n\n🔧 Адмін-команди:\n/admin - Панель керування"
    
    await update.message.reply_text(welcome_text, reply_markup=get_keyboard())

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-панель"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас немає доступу до цієї команди")
        return
    
    await update.message.reply_text(
        "🔧 Адмін-панель активована!\n\n"
        "Використовуйте кнопки нижче для керування ботом 👇",
        reply_markup=get_admin_keyboard()
    )

async def set_privilege(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Встановити привілеї користувачу"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас немає доступу")
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("❌ Використання: /setpriv [user_id] [level 0-9]")
        return
    
    try:
        target_user_id = int(context.args[0])
        privilege_level = int(context.args[1])
        
        if privilege_level < 0 or privilege_level > 9:
            await update.message.reply_text("❌ Рівень привілеїв має бути від 0 до 9")
            return
        
        user_data = get_user_data(target_user_id)
        user_data["privilege_level"] = privilege_level
        update_user_data(target_user_id, user_data)
        
        await update.message.reply_text(
            f"✅ Привілеї встановлено!\n\n"
            f"👤 Користувач: {target_user_id}\n"
            f"⭐ Рівень: {privilege_level}\n"
            f"{'🚀 Без затримки!' if privilege_level == 9 else ''}"
        )
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Використовуйте числа.")

async def set_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Встановити затримку користувачу"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас немає доступу")
        return
    
    if len(context.args) != 2:
        await update.message.reply_text("❌ Використання: /setcooldown [user_id] [seconds]")
        return
    
    try:
        target_user_id = int(context.args[0])
        cooldown = int(context.args[1])
        
        if cooldown < 0:
            await update.message.reply_text("❌ Затримка не може бути негативною")
            return
        
        user_data = get_user_data(target_user_id)
        user_data["custom_cooldown"] = cooldown if cooldown > 0 else None
        update_user_data(target_user_id, user_data)
        
        await update.message.reply_text(
            f"✅ Затримку встановлено!\n\n"
            f"👤 Користувач: {target_user_id}\n"
            f"⏱️ Затримка: {cooldown} секунд"
        )
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Використовуйте числа.")

async def add_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Додати ліміти користувачу"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас немає доступу")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text("❌ Використання: /addlimit [user_id] [messages/photos/voice] [amount]")
        return
    
    try:
        target_user_id = int(context.args[0])
        limit_type = context.args[1]
        amount = int(context.args[2])
        
        if limit_type not in ["messages", "photos", "voice"]:
            await update.message.reply_text("❌ Тип має бути: messages, photos або voice")
            return
        
        user_data = get_user_data(target_user_id)
        current = DAILY_LIMITS[limit_type] - user_data["limits"][limit_type]["count"]
        user_data["limits"][limit_type]["count"] = max(0, user_data["limits"][limit_type]["count"] - amount)
        update_user_data(target_user_id, user_data)
        
        new_available = DAILY_LIMITS[limit_type] - user_data["limits"][limit_type]["count"]
        
        await update.message.reply_text(
            f"✅ Ліміти оновлено!\n\n"
            f"👤 Користувач: {target_user_id}\n"
            f"📊 Тип: {limit_type}\n"
            f"➕ Додано: {amount}\n"
            f"📈 Було: {current}\n"
            f"📊 Стало: {new_available}"
        )
    except ValueError:
        await update.message.reply_text("❌ Невірний формат.")

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Інформація про користувача"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас немає доступу")
        return
    
    if len(context.args) != 1:
        await update.message.reply_text("❌ Використання: /userinfo [user_id]")
        return
    
    try:
        target_user_id = int(context.args[0])
        user_data = get_user_data(target_user_id)
        limits = get_remaining_limits(target_user_id)
        
        await update.message.reply_text(
            f"👤 Інформація про користувача {target_user_id}\n\n"
            f"⭐ Привілеї: {user_data.get('privilege_level', 0)}\n"
            f"⏱️ Затримка: {user_data.get('custom_cooldown') or MESSAGE_COOLDOWN} сек\n\n"
            f"📊 Статистика:\n"
            f"💬 Повідомлень: {user_data['stats']['messages']}\n"
            f"🎤 Голосових: {user_data['stats']['voice']}\n"
            f"📷 Фото: {user_data['stats']['photos']}\n"
            f"🎨 Картинок: {user_data['stats']['images_generated']}\n\n"
            f"📅 Залишилось:\n"
            f"💬 Повідомлень: {limits['messages']['remaining']}/{DAILY_LIMITS['messages']}\n"
            f"🎤 Голосових: {limits['voice']['remaining']}/{DAILY_LIMITS['voice']}\n"
            f"📷 Фото: {limits['photos']['remaining']}/{DAILY_LIMITS['photos']}"
        )
    except ValueError:
        await update.message.reply_text("❌ Невірний ID користувача")

async def bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Загальна статистика бота"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас немає доступу")
        return
    
    total_users = len(database["users"])
    total_messages = sum(u["stats"]["messages"] for u in database["users"].values())
    total_voice = sum(u["stats"]["voice"] for u in database["users"].values())
    total_photos = sum(u["stats"]["photos"] for u in database["users"].values())
    total_images = sum(u["stats"]["images_generated"] for u in database["users"].values())
    
    await update.message.reply_text(
        f"📊 Статистика бота\n\n"
        f"👥 Всього користувачів: {total_users}\n\n"
        f"📈 Всього оброблено:\n"
        f"💬 Повідомлень: {total_messages}\n"
        f"🎤 Голосових: {total_voice}\n"
        f"📷 Фото: {total_photos}\n"
        f"🎨 Згенеровано картинок: {total_images}",
        reply_markup=get_admin_keyboard()
    )

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список користувачів"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        return
    
    users_list = []
    for uid, data in database["users"].items():
        first_seen = datetime.fromisoformat(data.get("first_seen", datetime.now().isoformat()))
        priv = data.get("privilege_level", 0)
        banned = "🚫" if data.get("banned", False) else ""
        username = f"@{data.get('username')}" if data.get('username') else ""
        users_list.append(f"{banned}ID: {uid} {username} | ⭐{priv} | {first_seen.strftime('%d.%m.%Y')}")
    
    if not users_list:
        await update.message.reply_text("Немає користувачів", reply_markup=get_admin_keyboard())
        return
    
    # Разбиваем на части если много
    chunk_size = 20
    for i in range(0, len(users_list), chunk_size):
        chunk = users_list[i:i+chunk_size]
        text = "👥 Користувачі:\n\n" + "\n".join(chunk)
        await update.message.reply_text(text, reply_markup=get_admin_keyboard())

async def show_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    """Показати інформацію про користувача"""
    user_data = get_user_data(target_user_id)
    limits = get_remaining_limits(target_user_id)
    first_seen = datetime.fromisoformat(user_data.get("first_seen", datetime.now().isoformat()))
    username = f"@{user_data.get('username')}" if user_data.get('username') else "Немає"
    
    await update.message.reply_text(
        f"👤 Користувач {target_user_id}\n"
        f"📝 Username: {username}\n\n"
        f"📅 Зайшов: {first_seen.strftime('%d.%m.%Y %H:%M')}\n"
        f"⭐ Привілеї: {user_data.get('privilege_level', 0)}\n"
        f"⏱️ Затримка: {user_data.get('custom_cooldown') or MESSAGE_COOLDOWN} сек\n"
        f"🚫 Бан: {'Так' if user_data.get('banned', False) else 'Ні'}\n\n"
        f"📊 Статистика:\n"
        f"💬 Повідомлень: {user_data['stats']['messages']}\n"
        f"🎤 Голосових: {user_data['stats']['voice']}\n"
        f"📷 Фото: {user_data['stats']['photos']}\n"
        f"🎨 Картинок: {user_data['stats']['images_generated']}\n\n"
        f"📅 Залишилось:\n"
        f"💬 Повідомлень: {limits['messages']['remaining']}/{DAILY_LIMITS['messages']}\n"
        f"🎤 Голосових: {limits['voice']['remaining']}/{DAILY_LIMITS['voice']}\n"
        f"📷 Фото: {limits['photos']['remaining']}/{DAILY_LIMITS['photos']}",
        reply_markup=get_admin_keyboard()
    )

async def set_user_privilege(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, level: int):
    """Встановити привілеї"""
    if level < 0 or level > 9:
        await update.message.reply_text("❌ Рівень 0-9", reply_markup=get_admin_keyboard())
        return
    
    user_data = get_user_data(target_user_id)
    user_data["privilege_level"] = level
    
    # Расширяем лимиты в зависимости от привилегий
    if level >= 1:
        multiplier = 1 + (level * 0.5)  # +50% за каждый уровень
        for limit_type in ["messages", "photos", "voice"]:
            user_data["limits"][limit_type]["count"] = 0
    
    update_user_data(target_user_id, user_data)
    
    await update.message.reply_text(
        f"✅ Привілеї встановлено!\n\n"
        f"👤 ID: {target_user_id}\n"
        f"⭐ Рівень: {level}\n"
        f"{'🚀 Без затримки!' if level == 9 else ''}",
        reply_markup=get_admin_keyboard()
    )

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    """Забанити користувача"""
    user_data = get_user_data(target_user_id)
    user_data["banned"] = True
    update_user_data(target_user_id, user_data)
    
    await update.message.reply_text(
        f"🚫 Користувача {target_user_id} заблоковано!",
        reply_markup=get_admin_keyboard()
    )

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int):
    """Розбанити користувача"""
    user_data = get_user_data(target_user_id)
    user_data["banned"] = False
    update_user_data(target_user_id, user_data)
    
    await update.message.reply_text(
        f"✅ Користувача {target_user_id} розблоковано!",
        reply_markup=get_admin_keyboard()
    )

async def set_user_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, cooldown: int):
    """Встановити затримку"""
    if cooldown < 0:
        await update.message.reply_text("❌ Затримка не може бути негативною", reply_markup=get_admin_keyboard())
        return
    
    user_data = get_user_data(target_user_id)
    user_data["custom_cooldown"] = cooldown if cooldown > 0 else None
    update_user_data(target_user_id, user_data)
    
    await update.message.reply_text(
        f"✅ Затримку встановлено!\n\n"
        f"👤 ID: {target_user_id}\n"
        f"⏱️ Затримка: {cooldown} сек",
        reply_markup=get_admin_keyboard()
    )

async def add_user_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, limit_type: str, amount: int):
    """Додати ліміти"""
    if limit_type not in ["messages", "photos", "voice"]:
        await update.message.reply_text("❌ Тип: messages, photos або voice", reply_markup=get_admin_keyboard())
        return
    
    user_data = get_user_data(target_user_id)
    current = DAILY_LIMITS[limit_type] - user_data["limits"][limit_type]["count"]
    user_data["limits"][limit_type]["count"] = max(0, user_data["limits"][limit_type]["count"] - amount)
    update_user_data(target_user_id, user_data)
    
    new_available = DAILY_LIMITS[limit_type] - user_data["limits"][limit_type]["count"]
    
    await update.message.reply_text(
        f"✅ Ліміти оновлено!\n\n"
        f"👤 ID: {target_user_id}\n"
        f"📊 Тип: {limit_type}\n"
        f"➕ Додано: {amount}\n"
        f"📈 Було: {current} → Стало: {new_available}",
        reply_markup=get_admin_keyboard()
    )

# Настройка голосовых ответов для каждого пользователя
voice_enabled = defaultdict(lambda: False)
user_stats = defaultdict(lambda: {"messages": 0, "voice": 0, "photos": 0, "images_generated": 0})

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if query.data == "toggle_voice":
        voice_enabled[user_id] = not voice_enabled[user_id]
        status = "увімкнено ✅" if voice_enabled[user_id] else "вимкнено ❌"
        await query.edit_message_text(f"Голосові відповіді {status}")
        
    elif query.data == "generate_image":
        await query.edit_message_text(
            "🎨 Генерація картинок\n\n"
            "Напишіть команду:\n"
            "/draw опис картинки\n\n"
            "Наприклад:\n"
            "/draw кіт в космосі"
        )
        
    elif query.data == "clear_history":
        user_conversations[user_id].clear()
        await query.edit_message_text("🗑️ Історію очищено!")
        
    elif query.data == "show_stats":
        stats = user_stats[user_id]
        await query.edit_message_text(
            f"📊 Ваша статистика:\n\n"
            f"💬 Повідомлень: {stats['messages']}\n"
            f"🎤 Голосових: {stats['voice']}\n"
            f"📷 Фото: {stats['photos']}\n"
            f"🎨 Згенеровано картинок: {stats['images_generated']}"
        )

async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация изображений"""
    user_id = update.effective_user.id
    
    # Обновляем username
    if update.effective_user.username:
        update_username(user_id, update.effective_user.username)
    
    # Проверка бана
    user_data = get_user_data(user_id)
    if user_data.get("banned", False):
        await update.message.reply_text("🚫 Ви заблоковані адміністратором")
        return
    
    if not context.args:
        await update.message.reply_text(
            "🎨 Використання:\n"
            "/draw опис картинки\n\n"
            "Наприклад:\n"
            "/draw кіт в космосі\n"
            "/draw красивий закат над морем",
            reply_markup=get_keyboard()
        )
        return
    
    prompt = " ".join(context.args)
    status_msg = await update.message.reply_text("🎨 Малюю картинку... ✨")
    
    try:
        # Список сервисов для генерации (бесплатные)
        services = [
            {
                "name": "Pollinations AI",
                "url": f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true"
            },
            {
                "name": "Hugging Face",
                "url": f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?model=flux&width=1024&height=1024&nologo=true"
            }
        ]
        
        image_generated = False
        
        for service in services:
            try:
                logger.info(f"Пробуем сервис: {service['name']}")
                response = requests.get(service['url'], timeout=60)
                
                if response.status_code == 200 and len(response.content) > 1000:
                    # Сохраняем временно
                    img_path = os.path.join(tempfile.gettempdir(), f"generated_{user_id}.jpg")
                    with open(img_path, "wb") as f:
                        f.write(response.content)
                    
                    await status_msg.delete()
                    await update.message.reply_photo(
                        photo=open(img_path, "rb"),
                        caption=f"🎨 {prompt}",
                        reply_markup=get_keyboard()
                    )
                    
                    os.remove(img_path)
                    user_data = get_user_data(user_id)
                    user_data["stats"]["images_generated"] += 1
                    update_user_data(user_id, user_data)
                    
                    image_generated = True
                    logger.info(f"Изображение создано через {service['name']}")
                    break
                else:
                    logger.warning(f"{service['name']} вернул ошибку {response.status_code}")
                    continue
                    
            except Exception as e:
                logger.error(f"Ошибка с {service['name']}: {e}")
                continue
        
        if not image_generated:
            await status_msg.edit_text("❌ Не вдалося згенерувати картинку. Спробуйте ще раз або інший опис.")
            
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        await status_msg.edit_text("❌ Помилка генерації. Спробуйте інший опис.")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка истории"""
    user_id = update.effective_user.id
    user_conversations[user_id].clear()
    await update.message.reply_text("История очищена!")

async def toggle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включить/выключить голосовые ответы"""
    user_id = update.effective_user.id
    voice_enabled[user_id] = not voice_enabled[user_id]
    status = "увімкнено ✅" if voice_enabled[user_id] else "вимкнено ❌"
    await update.message.reply_text(f"Голосові відповіді {status}")

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать историю"""
    user_id = update.effective_user.id
    history = user_conversations[user_id]
    
    if not history:
        await update.message.reply_text("История пуста")
        return
    
    text = "📝 История диалога:\n\n"
    for msg in history[-5:]:  # Последние 5
        role = "Вы" if msg["role"] == "user" else "Бот"
        text += f"{role}: {msg['content'][:100]}...\n\n"
    
    await update.message.reply_text(text)

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений и кнопок"""
    user_id = update.effective_user.id
    user_message = update.message.text
    
    # Обновляем username если есть
    if update.effective_user.username:
        update_username(user_id, update.effective_user.username)
    
    # Проверка бана
    user_data = get_user_data(user_id)
    if user_data.get("banned", False):
        await update.message.reply_text("🚫 Ви заблоковані адміністратором")
        return
    
    # Админские кнопки
    if user_id == ADMIN_ID:
        if user_message == "📊 Статистика бота":
            await bot_stats(update, context)
            return
        elif user_message == "👥 Список користувачів":
            await list_users(update, context)
            return
        elif user_message == "🔍 Інфо про юзера":
            await update.message.reply_text(
                "Введіть ID або @username:\n\nНаприклад:\n123456789\n@username",
                reply_markup=get_admin_keyboard()
            )
            context.user_data["waiting_for"] = "user_info"
            return
        elif user_message == "⭐ Видати привілеї":
            await update.message.reply_text(
                "Введіть: [ID/@username] [рівень 0-9]\n\n"
                "Наприклад:\n123456789 9\n@username 9\n\n"
                "Привілеї:\n"
                "0 = Звичайний\n"
                "1-3 = Більше повідомлень\n"
                "4-6 = Більше фото/голосу\n"
                "7-8 = Більше всього\n"
                "9 = Без затримки",
                reply_markup=get_admin_keyboard()
            )
            context.user_data["waiting_for"] = "set_priv"
            return
        elif user_message == "🚫 Забанити":
            await update.message.reply_text(
                "Введіть ID або @username для бану:\n\nНаприклад:\n123456789\n@username",
                reply_markup=get_admin_keyboard()
            )
            context.user_data["waiting_for"] = "ban_user"
            return
        elif user_message == "✅ Розбанити":
            await update.message.reply_text(
                "Введіть ID або @username для розбану:\n\nНаприклад:\n123456789\n@username",
                reply_markup=get_admin_keyboard()
            )
            context.user_data["waiting_for"] = "unban_user"
            return
        elif user_message == "⏱️ Змінити затримку":
            await update.message.reply_text(
                "Введіть: [ID/@username] [секунди]\n\nНаприклад:\n123456789 5\n@username 5",
                reply_markup=get_admin_keyboard()
            )
            context.user_data["waiting_for"] = "set_cooldown"
            return
        elif user_message == "➕ Додати ліміти":
            await update.message.reply_text(
                "Введіть: [ID/@username] [messages/photos/voice] [кількість]\n\n"
                "Наприклад:\n123456789 messages 50\n@username messages 50",
                reply_markup=get_admin_keyboard()
            )
            context.user_data["waiting_for"] = "add_limit"
            return
        elif user_message == "🔙 Звичайна клавіатура":
            await update.message.reply_text(
                "Повернуто звичайну клавіатуру ✅",
                reply_markup=get_keyboard()
            )
            return
        
        # Обработка ожидаемых данных
        if "waiting_for" in context.user_data:
            action = context.user_data["waiting_for"]
            del context.user_data["waiting_for"]
            
            if action == "user_info":
                target_id = parse_user_identifier(user_message)
                if target_id:
                    await show_user_info(update, context, target_id)
                else:
                    await update.message.reply_text("❌ Користувача не знайдено", reply_markup=get_admin_keyboard())
                return
            elif action == "set_priv":
                parts = user_message.split()
                if len(parts) == 2:
                    target_id = parse_user_identifier(parts[0])
                    if target_id:
                        try:
                            level = int(parts[1])
                            await set_user_privilege(update, context, target_id, level)
                        except ValueError:
                            await update.message.reply_text("❌ Невірний рівень", reply_markup=get_admin_keyboard())
                    else:
                        await update.message.reply_text("❌ Користувача не знайдено", reply_markup=get_admin_keyboard())
                else:
                    await update.message.reply_text("❌ Невірний формат", reply_markup=get_admin_keyboard())
                return
            elif action == "ban_user":
                target_id = parse_user_identifier(user_message)
                if target_id:
                    await ban_user(update, context, target_id)
                else:
                    await update.message.reply_text("❌ Користувача не знайдено", reply_markup=get_admin_keyboard())
                return
            elif action == "unban_user":
                target_id = parse_user_identifier(user_message)
                if target_id:
                    await unban_user(update, context, target_id)
                else:
                    await update.message.reply_text("❌ Користувача не знайдено", reply_markup=get_admin_keyboard())
                return
            elif action == "set_cooldown":
                parts = user_message.split()
                if len(parts) == 2:
                    target_id = parse_user_identifier(parts[0])
                    if target_id:
                        try:
                            cooldown = int(parts[1])
                            await set_user_cooldown(update, context, target_id, cooldown)
                        except ValueError:
                            await update.message.reply_text("❌ Невірна затримка", reply_markup=get_admin_keyboard())
                    else:
                        await update.message.reply_text("❌ Користувача не знайдено", reply_markup=get_admin_keyboard())
                else:
                    await update.message.reply_text("❌ Невірний формат", reply_markup=get_admin_keyboard())
                return
            elif action == "add_limit":
                parts = user_message.split()
                if len(parts) == 3:
                    target_id = parse_user_identifier(parts[0])
                    if target_id:
                        try:
                            limit_type = parts[1]
                            amount = int(parts[2])
                            await add_user_limit(update, context, target_id, limit_type, amount)
                        except ValueError:
                            await update.message.reply_text("❌ Невірна кількість", reply_markup=get_admin_keyboard())
                    else:
                        await update.message.reply_text("❌ Користувача не знайдено", reply_markup=get_admin_keyboard())
                else:
                    await update.message.reply_text("❌ Невірний формат", reply_markup=get_admin_keyboard())
                return
    
    # Обработка ожидания описания для рисования (для обычных пользователей)
    if "waiting_for" in context.user_data and context.user_data["waiting_for"] == "draw_image":
        del context.user_data["waiting_for"]
        
        # Проверка бана
        user_data = get_user_data(user_id)
        if user_data.get("banned", False):
            await update.message.reply_text("🚫 Ви заблоковані адміністратором")
            return
        
        prompt = user_message
        status_msg = await update.message.reply_text("🎨 Малюю картинку... ✨")
        
        try:
            # Список сервисов для генерации (бесплатные)
            services = [
                {
                    "name": "Pollinations AI",
                    "url": f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true"
                },
                {
                    "name": "Hugging Face",
                    "url": f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?model=flux&width=1024&height=1024&nologo=true"
                }
            ]
            
            image_generated = False
            
            for service in services:
                try:
                    logger.info(f"Пробуем сервис: {service['name']}")
                    response = requests.get(service['url'], timeout=60)
                    
                    if response.status_code == 200 and len(response.content) > 1000:
                        # Сохраняем временно
                        img_path = os.path.join(tempfile.gettempdir(), f"generated_{user_id}.jpg")
                        with open(img_path, "wb") as f:
                            f.write(response.content)
                        
                        await status_msg.delete()
                        await update.message.reply_photo(
                            photo=open(img_path, "rb"),
                            caption=f"🎨 {prompt}",
                            reply_markup=get_keyboard()
                        )
                        
                        os.remove(img_path)
                        user_data = get_user_data(user_id)
                        user_data["stats"]["images_generated"] += 1
                        update_user_data(user_id, user_data)
                        
                        image_generated = True
                        logger.info(f"Изображение создано через {service['name']}")
                        break
                    else:
                        logger.warning(f"{service['name']} вернул ошибку {response.status_code}")
                        continue
                        
                except Exception as e:
                    logger.error(f"Ошибка с {service['name']}: {e}")
                    continue
            
            if not image_generated:
                await status_msg.edit_text("❌ Не вдалося згенерувати картинку. Спробуйте ще раз або інший опис.")
                
        except Exception as e:
            logger.error(f"Ошибка генерации изображения: {e}")
            await status_msg.edit_text("❌ Помилка генерації. Спробуйте інший опис.")
        return
    
    # Обработка нажатий на кнопки клавиатуры
    if user_message == "🎨 Намалювати":
        await update.message.reply_text(
            "🎨✨ Чудово! Опишіть що намалювати:\n\n"
            "Наприклад:\n"
            "• кіт в космосі 🚀\n"
            "• красивий закат над морем 🌅\n"
            "• фантастичний замок 🏰",
            reply_markup=get_keyboard()
        )
        context.user_data["waiting_for"] = "draw_image"
        return
    elif user_message == "🎤 Голос вкл/викл":
        user_data = get_user_data(user_id)
        user_data["voice_enabled"] = not user_data["voice_enabled"]
        update_user_data(user_id, user_data)
        status = "увімкнено ✅" if user_data["voice_enabled"] else "вимкнено ❌"
        emoji = "🔊" if user_data["voice_enabled"] else "🔇"
        await update.message.reply_text(f"{emoji} Голосові відповіді {status}", reply_markup=get_keyboard())
        return
    elif user_message == "📊 Статистика":
        user_data = get_user_data(user_id)
        stats = user_data["stats"]
        limits = get_remaining_limits(user_id)
        await update.message.reply_text(
            f"📊 Ваша статистика:\n\n"
            f"💬 Повідомлень: {stats['messages']}\n"
            f"🎤 Голосових: {stats['voice']}\n"
            f"📷 Фото: {stats['photos']}\n"
            f"🎨 Згенеровано картинок: {stats['images_generated']}\n\n"
            f"📅 Залишилось:\n"
            f"💬 Повідомлень: {limits['messages']['remaining']}/{DAILY_LIMITS['messages']} (⏰ {format_time_remaining(limits['messages']['time_left'])})\n"
            f"🎤 Голосових: {limits['voice']['remaining']}/{DAILY_LIMITS['voice']} (⏰ {format_time_remaining(limits['voice']['time_left'])})\n"
            f"📷 Фото: {limits['photos']['remaining']}/{DAILY_LIMITS['photos']} (⏰ {format_time_remaining(limits['photos']['time_left'])})\n\n"
            f"Дякую що користуєтесь ботом! 😊",
            reply_markup=get_keyboard()
        )
        return
    elif user_message == "🗑️ Очистити":
        user_conversations[user_id].clear()
        await update.message.reply_text("🗑️✨ Історію очищено! Почнемо спілкування з чистого аркуша! 😊", reply_markup=get_keyboard())
        return
    
    user_stats[user_id]["messages"] += 1
    await process_text_message(update, context, user_message)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка голосовых сообщений"""
    user_id = update.effective_user.id
    
    # Проверка затримки
    can_send, remaining = check_cooldown(user_id)
    if not can_send:
        await update.message.reply_text(
            f"⏳ Зачекайте {remaining} секунд перед наступним повідомленням",
            reply_markup=get_keyboard()
        )
        return
    
    # Проверка лимита
    if not check_and_update_limit(user_id, "voice"):
        limits = get_remaining_limits(user_id)
        await update.message.reply_text(
            f"❌ Ліміт голосових повідомлень вичерпано!\n\n"
            f"Ліміт: {DAILY_LIMITS['voice']} голосових на 12 годин\n"
            f"⏰ До скидання: {format_time_remaining(limits['voice']['time_left'])}",
            reply_markup=get_keyboard()
        )
        return
    
    user_stats[user_id]["voice"] += 1
    
    status_msg = await update.message.reply_text("🎤 Слухаю...")
    
    try:
        # Скачиваем голосовое сообщение
        voice_file = await update.message.voice.get_file()
        voice_path = os.path.join(tempfile.gettempdir(), f"voice_{user_id}.ogg")
        await voice_file.download_to_drive(voice_path)
        
        # Конвертируем в WAV
        wav_path = os.path.join(tempfile.gettempdir(), f"voice_{user_id}.wav")
        audio = AudioSegment.from_file(voice_path, format="ogg")
        
        # Улучшаем качество аудио максимально
        audio = audio.set_frame_rate(16000)
        audio = audio.set_channels(1)
        audio = audio.set_sample_width(2)
        audio = audio + 10  # Увеличиваем громкость
        audio.export(wav_path, format="wav")
        
        # Распознаем речь с улучшенными настройками
        recognizer = sr.Recognizer()
        recognizer.energy_threshold = 200
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold = 1.0
        recognizer.phrase_threshold = 0.3
        recognizer.non_speaking_duration = 0.5
        
        text = None
        detected_lang = "uk"
        
        with sr.AudioFile(wav_path) as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.3)
            audio_data = recognizer.record(source)
            
            # Пробуем оба языка
            languages = [("uk-UA", "uk"), ("ru-RU", "ru")]
            
            for lang_code, lang_short in languages:
                try:
                    text = recognizer.recognize_google(audio_data, language=lang_code, show_all=False)
                    detected_lang = lang_short
                    break
                except (sr.UnknownValueError, sr.RequestError):
                    continue
            
            if not text:
                raise Exception("Не вдалося розпізнати")
        
        # Удаляем временные файлы
        os.remove(voice_path)
        os.remove(wav_path)
        
        lang_emoji = "🇺🇦" if detected_lang == "uk" else "🇷🇺"
        await status_msg.edit_text(f"{lang_emoji} Ви сказали: {text}" if detected_lang == "uk" else f"{lang_emoji} Вы сказали: {text}")
        
        # Обрабатываем распознанный текст
        await process_text_message(update, context, text)
        
    except Exception as e:
        logger.error(f"Ошибка распознавания: {e}")
        await status_msg.edit_text("❌ Не вдалося розпізнати голос.\n\n💡 Поради:\n• Говоріть чіткіше\n• Говоріть голосніше\n• Уникайте шуму\n• Спробуйте ще раз")

async def process_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    """Обработка текстового сообщения (для голоса и текста)"""
    user_id = update.effective_user.id
    
    # Проверка затримки
    can_send, remaining = check_cooldown(user_id)
    if not can_send:
        await update.message.reply_text(
            f"⏳ Зачекайте {remaining} секунд перед наступним повідомленням 😊\n"
            f"Я тут, нікуди не поспішаю! ❤️",
            reply_markup=get_keyboard()
        )
        return
    
    # Проверка лимита (только для обычных текстовых сообщений, не для голосовых)
    if not check_and_update_limit(user_id, "messages"):
        limits = get_remaining_limits(user_id)
        await update.message.reply_text(
            f"❌ Ліміт повідомлень вичерпано! 😔\n\n"
            f"Ліміт: {DAILY_LIMITS['messages']} повідомлень на 12 годин\n"
            f"⏰ До скидання: {format_time_remaining(limits['messages']['time_left'])}\n\n"
            f"Але не засмучуйтесь, я скоро повернусь! 😊✨",
            reply_markup=get_keyboard()
        )
        return
    
    await update.message.chat.send_action(action="typing")
    status_msg = await update.message.reply_text("⏳ Думаю над відповіддю... 🤔")
    
    try:
        # Определяем язык сообщения
        is_ukrainian = any(char in user_message for char in "іїєґІЇЄҐ")
        
        # Системный промпт в зависимости от языка
        system_prompt = {
            "role": "system",
            "content": "Ти дружелюбний та добрий асистент. Відповідай українською мовою тепло та привітно. Використовуй емодзі природно (2-4 на повідомлення) щоб передати емоції та зробити спілкування приємнішим. Будь уважним до користувача, підтримуй його, радій разом з ним. Використовуй правильну граматику та орфографію. Емодзі додавай там де це підходить за контекстом: 😊 для привітності, 👍 для схвалення, 🎉 для радості, 💡 для ідей, ❤️ для підтримки, 🤔 для роздумів, ✨ для чогось особливого." if is_ukrainian else "Ты дружелюбный и добрый ассистент. Отвечай на русском языке тепло и приветливо. Используй эмодзи естественно (2-4 на сообщение) чтобы передать эмоции и сделать общение приятнее. Будь внимательным к пользователю, поддерживай его, радуйся вместе с ним. Используй правильную грамматику и орфографию. Эмодзи добавляй там где это подходит по контексту: 😊 для приветливости, 👍 для одобрения, 🎉 для радости, 💡 для идей, ❤️ для поддержки, 🤔 для размышлений, ✨ для чего-то особенного."
        }
        
        # Добавляем сообщение пользователя в историю
        user_conversations[user_id].append({
            "role": "user",
            "content": user_message
        })
        
        # Ограничиваем историю
        if len(user_conversations[user_id]) > MAX_HISTORY * 2:
            user_conversations[user_id] = user_conversations[user_id][-MAX_HISTORY * 2:]
        
        # Формируем сообщения с системным промптом
        messages = [system_prompt] + user_conversations[user_id]
        
        # Запрос к Groq API с историей
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 300
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            bot_reply = result["choices"][0]["message"]["content"]
            
            # Добавляем ответ бота в историю
            user_conversations[user_id].append({
                "role": "assistant",
                "content": bot_reply
            })
            
            # Удаляем сообщение "печатает..."
            await status_msg.delete()
            
            # Отправляем текстовый ответ с клавиатурой
            await update.message.reply_text(bot_reply, reply_markup=get_keyboard())
            
            # Если включены голосовые ответы, отправляем голос
            if get_user_data(user_id)["voice_enabled"]:
                await update.message.chat.send_action(action="record_voice")
                voice_lang = "uk" if is_ukrainian else "ru"
                voice_file = text_to_speech(bot_reply, voice_lang)
                if voice_file:
                    await update.message.reply_voice(voice=open(voice_file, 'rb'))
                    os.remove(voice_file)
        else:
            await status_msg.edit_text(f"Ошибка API: {response.status_code}")
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await status_msg.edit_text("Произошла ошибка. Попробуйте позже.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Реальное распознавание фото через Google Gemini"""
    user_id = update.effective_user.id
    
    # Проверка затримки
    can_send, remaining = check_cooldown(user_id)
    if not can_send:
        await update.message.reply_text(
            f"⏳ Зачекайте {remaining} секунд перед наступним повідомленням",
            reply_markup=get_keyboard()
        )
        return
    
    # Проверка лимита
    if not check_and_update_limit(user_id, "photos"):
        limits = get_remaining_limits(user_id)
        await update.message.reply_text(
            f"❌ Ліміт фото вичерпано!\n\n"
            f"Ліміт: {DAILY_LIMITS['photos']} фото на 12 годин\n"
            f"⏰ До скидання: {format_time_remaining(limits['photos']['time_left'])}",
            reply_markup=get_keyboard()
        )
        return
    
    user_stats[user_id]["photos"] += 1
    
    status_msg = await update.message.reply_text("📷 Аналізую фото...")
    
    try:
        # Получаем фото
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_path = os.path.join(tempfile.gettempdir(), f"photo_{user_id}.jpg")
        await photo_file.download_to_drive(photo_path)
        
        # Сжимаем и конвертируем в base64
        img = Image.open(photo_path)
        img.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        os.remove(photo_path)
        
        # Получаем подпись
        caption = update.message.caption or ""
        is_ukrainian = any(char in caption for char in "іїєґІЇЄҐ")
        
        # Формируем точный промпт
        if caption:
            prompt = caption + (" Детально опиши що ти бачиш на фото українською мовою." if is_ukrainian else " Детально опиши что ты видишь на фото на русском языке.")
        else:
            prompt = "Детально опиши що ти бачиш на цьому фото. Назви всі об'єкти, кольори, текст якщо є." if is_ukrainian else "Детально опиши что ты видишь на этом фото. Назови все объекты, цвета, текст если есть."
        
        # Список моделей для попытки (от лучшей к запасной)
        models = [
            "gemini-2.0-flash-exp",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-1.5-pro",
            "gemini-pro-vision",
            "gemini-1.5-flash-latest",
            "gemini-2.5-flash-lite",
            "gemini-3-flash",
            "gemini-3-1b",
            "gemini-3-4b",
            "gemini-3-12b",
            "gemini-3-27b",
            "gemini-3-2b"
        ]
        
        description = None
        
        for model in models:
            try:
                # Используем Google Gemini API
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GOOGLE_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{
                            "parts": [
                                {"text": prompt},
                                {
                                    "inline_data": {
                                        "mime_type": "image/jpeg",
                                        "data": image_base64
                                    }
                                }
                            ]
                        }],
                        "generationConfig": {
                            "temperature": 0.2,
                            "maxOutputTokens": 2048
                        }
                    },
                    timeout=60
                )
                
                if response.status_code == 200:
                    result = response.json()
                    description = result["candidates"][0]["content"]["parts"][0]["text"]
                    logger.info(f"Фото распознано моделью {model}")
                    break
                elif response.status_code == 429:
                    logger.warning(f"Модель {model} досягла ліміту (429), пробуємо наступну")
                    continue
                else:
                    logger.warning(f"Модель {model} вернула ошибку {response.status_code}, пробуем следующую")
                    continue
                    
            except Exception as e:
                logger.error(f"Ошибка с моделью {model}: {e}")
                continue
        
        if description:
            await status_msg.delete()
            
            # Если описание слишком длинное, разбиваем на части
            if len(description) > 4000:
                parts = [description[i:i+4000] for i in range(0, len(description), 4000)]
                for i, part in enumerate(parts):
                    if i == 0:
                        await update.message.reply_text(f"📷 {part}", reply_markup=get_keyboard())
                    else:
                        await update.message.reply_text(part, reply_markup=get_keyboard())
            else:
                await update.message.reply_text(f"📷 {description}", reply_markup=get_keyboard())
            
            # Голосовой ответ
            if get_user_data(user_id)["voice_enabled"]:
                await update.message.chat.send_action(action="record_voice")
                voice_lang = "uk" if is_ukrainian else "ru"
                # Для голоса берем только первые 500 символов
                voice_text = description[:500] if len(description) > 500 else description
                voice_file = text_to_speech(voice_text, voice_lang)
                if voice_file:
                    await update.message.reply_voice(voice=open(voice_file, 'rb'))
                    os.remove(voice_file)
        else:
            await status_msg.edit_text(
                f"❌ Всі моделі розпізнавання недоступні\n\n"
                f"Спробуйте пізніше або перевірте API ключ на https://aistudio.google.com/"
            )
            
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}")
        await status_msg.edit_text("Не вдалося обробити фото")

def text_to_speech(text, lang='ru'):
    """Преобразование текста в голос"""
    try:
        tts = gTTS(text=text, lang=lang)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
        tts.save(temp_file.name)
        return temp_file.name
    except Exception as e:
        logger.error(f"Ошибка TTS: {e}")
        return None

def main():
    """Запуск бота"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("setpriv", set_privilege))
    application.add_handler(CommandHandler("setcooldown", set_cooldown))
    application.add_handler(CommandHandler("addlimit", add_limit))
    application.add_handler(CommandHandler("userinfo", user_info))
    application.add_handler(CommandHandler("stats", bot_stats))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("history", show_history))
    application.add_handler(CommandHandler("voice", toggle_voice))
    application.add_handler(CommandHandler("draw", draw_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    
    logger.info("Бот запущен с кнопками и генерацией изображений!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
