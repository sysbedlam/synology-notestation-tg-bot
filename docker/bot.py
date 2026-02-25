"""
Telegram Bot → Synology Note Station
-------------------------------------
Saves Telegram messages as notes in Synology Note Station.
Сохраняет сообщения Telegram как заметки в Synology Note Station.

Supported / Поддерживается:
  - Text / Текст
  - Photos / Фото
  - Albums (multiple photos) / Альбомы (несколько фото)
  - Files / Файлы
  - Clickable links / Кликабельные ссылки
  - Tags via /tag or hashtags / Теги через /tag или хэштеги
"""

import logging
import os
import asyncio
import base64
import json
import re
import requests
import urllib3
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# Suppress SSL warnings for self-signed certificates
# Подавляем предупреждения SSL для самоподписанных сертификатов
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
# SETTINGS — loaded from environment variables
# НАСТРОЙКИ — берутся из переменных окружения
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SYNOLOGY_HOST      = os.environ.get("SYNOLOGY_HOST", "https://nas:5001")
SYNOLOGY_USER      = os.environ.get("SYNOLOGY_USER", "")
SYNOLOGY_PASS      = os.environ.get("SYNOLOGY_PASS", "")
NOTE_NOTEBOOK      = os.environ.get("NOTE_NOTEBOOK", "Телеграм")
ALLOWED_USER_ID    = int(os.environ.get("ALLOWED_USER_ID", "0"))

# Temp directory for downloading files from Telegram
# Временная папка для скачивания файлов из Telegram
TEMP_DIR = "/tmp/tg_bot_files"

# Delay to collect all photos in an album before saving
# Задержка для сбора всех фото альбома перед сохранением
ALBUM_COLLECT_DELAY = 2.0
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

os.makedirs(TEMP_DIR, exist_ok=True)

# Persistent HTTP session for Synology API
# Постоянная HTTP сессия для Synology API
session = requests.Session()
syno_sid = None

# Album grouping storage: media_group_id -> list of messages
# Хранилище для группировки альбомов: media_group_id -> список сообщений
album_groups = {}
album_timers = {}

# Pending tags to apply to the next message
# Теги ожидающие следующего сообщения
pending_tags = []


# ══════════════════════════════════════════════
# Synology API
# ══════════════════════════════════════════════

def syno_login() -> bool:
    """
    Authenticate with Synology and store session ID.
    Авторизация в Synology и сохранение ID сессии.
    """
    global syno_sid
    url = f"{SYNOLOGY_HOST}/webapi/auth.cgi"
    params = {
        "api": "SYNO.API.Auth",
        "version": "3",
        "method": "login",
        "account": SYNOLOGY_USER,
        "passwd": SYNOLOGY_PASS,
        "session": "NoteStation",
        "format": "sid",
    }
    try:
        r = session.get(url, params=params, verify=False, timeout=10)
        data = r.json()
        if data.get("success"):
            syno_sid = data["data"]["sid"]
            logger.info("Synology: авторизация успешна / login successful")
            return True
        logger.error(f"Synology: ошибка авторизации / login error: {data}")
    except Exception as e:
        logger.error(f"Synology: ошибка подключения / connection error: {e}")
    return False


def ensure_auth() -> bool:
    """
    Ensure we have a valid session, re-login if needed.
    Проверяем наличие сессии, при необходимости логинимся заново.
    """
    global syno_sid
    if syno_sid:
        return True
    return syno_login()


def get_or_create_notebook(name: str) -> object:
    """
    Find notebook by name or create it if not found.
    Найти блокнот по имени или создать если не существует.
    Returns object_id or None.
    """
    url = f"{SYNOLOGY_HOST}/webapi/entry.cgi"
    try:
        r = session.get(url, params={
            "api": "SYNO.NoteStation.Notebook",
            "version": "2",
            "method": "list",
            "_sid": syno_sid,
        }, verify=False, timeout=10)
        data = r.json()
        if data.get("success"):
            for nb in data["data"].get("notebooks", []):
                if nb["title"] == name:
                    return nb["object_id"]

        # Notebook not found — create it / Блокнот не найден — создаём
        r = session.post(url, data={
            "api": "SYNO.NoteStation.Notebook",
            "version": "2",
            "method": "create",
            "title": name,
            "_sid": syno_sid,
        }, verify=False, timeout=10)
        data = r.json()
        if data.get("success"):
            return data["data"]["object_id"]
    except Exception as e:
        logger.error(f"get_or_create_notebook: {e}")
    return None


def attach_file_to_note(object_id: str, ver: str, file_path: str, filename: str) -> bool:
    """
    Attach a file to an existing note via multipart request.
    Прикрепляет файл к существующей заметке через multipart запрос.

    Note: api/version/method/_sid must be in URL query string (Note Station API quirk).
    Важно: api/version/method/_sid передаются в URL, а не в теле запроса.

    Note: object_id and ver must be wrapped in JSON quotes (Note Station API quirk).
    Важно: object_id и ver передаются в JSON кавычках — особенность Note Station API.
    """
    import mimetypes
    import random

    url = f"{SYNOLOGY_HOST}/webapi/entry.cgi?api=SYNO.NoteStation.Note&version=3&method=set&_sid={syno_sid}"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # Field name must match the name in attachment metadata
    # Имя поля должно совпадать с именем в метаданных вложения
    field_name = f"ext-gen{random.randint(1000, 9999)}"
    attachment_meta = json.dumps([{
        "action": "create",
        "format": "raw",
        "name": field_name,
    }])
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()

        data_fields = {
            "object_id": f'"{object_id}"',  # JSON quotes required / JSON кавычки обязательны
            "ver": f'"{ver}"',               # JSON quotes required / JSON кавычки обязательны
            "commit_msg": json.dumps({"device": "tgbot"}),
            "attachment": attachment_meta,
        }
        multipart = {
            field_name: (filename, file_data, mime),
        }
        r = session.post(url, data=data_fields, files=multipart, verify=False, timeout=60)
        data = r.json()
        if data.get("success"):
            logger.info(f"Файл прикреплён / file attached: {filename}")
            return True
        logger.error(f"Ошибка прикрепления файла / attach error: {data}")
    except Exception as e:
        logger.error(f"attach_file_to_note: {e}")
    return False


def create_note(title: str, html_content: str, attachment_ids: list = None):
    """
    Create a new note in Note Station.
    Создаёт новую заметку в Note Station.
    Returns (object_id, ver) or (None, None) on failure.
    Возвращает (object_id, ver) или (None, None) при ошибке.
    """
    if not ensure_auth():
        return None, None

    book_id = get_or_create_notebook(NOTE_NOTEBOOK)
    if not book_id:
        return None, None

    url = f"{SYNOLOGY_HOST}/webapi/entry.cgi"
    payload = {
        "api": "SYNO.NoteStation.Note",
        "version": "3",
        "method": "create",
        "parent_id": book_id,
        "title": title,
        "content": html_content,
        "commit_msg": '{"device":"tgbot","listable":false}',
        "encrypt": "false",
        "_sid": syno_sid,
    }
    if attachment_ids:
        payload["attachment_id"] = json.dumps(attachment_ids)

    try:
        r = session.post(url, data=payload, verify=False, timeout=15)
        data = r.json()
        if data.get("success"):
            logger.info(f"Заметка создана / note created: {title}")
            return data["data"]["object_id"], data["data"]["ver"]
        # Session expired — re-login and retry
        # Сессия истекла — переавторизуемся и повторяем
        if data.get("error", {}).get("code") in (105, 106, 119):
            syno_login()
            payload["_sid"] = syno_sid
            r = session.post(url, data=payload, verify=False, timeout=15)
            data = r.json()
            if data.get("success"):
                return data["data"]["object_id"], data["data"]["ver"]
        logger.error(f"Ошибка создания заметки / create error: {data}")
    except Exception as e:
        logger.error(f"create_note: {e}")
    return None, None


def set_note_tags(object_id: str, ver: str, tags: list) -> bool:
    """
    Set tags on an existing note.
    Устанавливает теги для существующей заметки.
    """
    if not tags:
        return True

    # api/version/method/_sid in URL — Note Station API quirk
    # api/version/method/_sid в URL — особенность Note Station API
    url = f"{SYNOLOGY_HOST}/webapi/entry.cgi?api=SYNO.NoteStation.Note&version=3&method=set&_sid={syno_sid}"
    payload = {
        "object_id": f'"{object_id}"',  # JSON quotes required / JSON кавычки обязательны
        "ver": f'"{ver}"',               # JSON quotes required / JSON кавычки обязательны
        "tag": json.dumps(tags),
        "commit_msg": '{"device":"tgbot"}',
        "check_conflict": "false",
    }
    try:
        r = session.post(url, data=payload, verify=False, timeout=15)
        data = r.json()
        if data.get("success"):
            logger.info(f"Теги установлены / tags set: {tags}")
            return True
        logger.error(f"Ошибка установки тегов / tags error: {data}")
    except Exception as e:
        logger.error(f"set_note_tags: {e}")
    return False


# ══════════════════════════════════════════════
# Utilities / Утилиты
# ══════════════════════════════════════════════

def text_to_html(text: str) -> str:
    """
    Convert plain text to HTML, making links clickable.
    Конвертирует текст в HTML, делая ссылки кликабельными.
    """
    import html
    escaped = html.escape(text)
    # Make URLs clickable / Делаем ссылки кликабельными
    escaped = re.sub(r'(https?://\S+)', r'<a href="\1">\1</a>', escaped)
    return "<p>" + escaped.replace("\n", "</p><p>") + "</p>"


async def download_tg_file(file_obj, context, filename: str) -> object:
    """
    Download a file from Telegram to temp directory.
    Скачивает файл из Telegram во временную директорию.
    Returns local file path or None on error.
    Возвращает путь к файлу или None при ошибке.
    """
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        path = os.path.join(TEMP_DIR, filename)
        await tg_file.download_to_drive(path)
        return path
    except Exception as e:
        logger.error(f"Ошибка скачивания / download error: {e}")
    return None


def extract_tags(text: str) -> list:
    """
    Extract hashtags from the last line of text.
    Only if the entire last line consists of hashtags.
    Извлекает хэштеги из последней строки текста.
    Только если последняя строка состоит исключительно из хэштегов.
    """
    if not text.strip():
        return []
    lines = text.strip().splitlines()
    last_line = lines[-1].strip()
    if re.match(r'^(#\S+\s*)+$', last_line):
        return [tag.lstrip('#') for tag in last_line.split()]
    return []


# ══════════════════════════════════════════════
# Album handling / Обработка альбомов
# ══════════════════════════════════════════════

async def process_album(group_id: str, context):
    """
    Wait for all photos in an album to arrive, then save as one note.
    Ждёт все фото альбома и сохраняет их как одну заметку.
    """
    await asyncio.sleep(ALBUM_COLLECT_DELAY)

    messages = album_groups.pop(group_id, [])
    album_timers.pop(group_id, None)

    if not messages:
        return

    # Get caption from first message that has one
    # Берём подпись из первого сообщения с подписью
    text = ""
    for m in messages:
        if m.caption:
            text = m.caption
            break

    # Download and encode all photos
    # Скачиваем и кодируем все фото
    photos_html = ""
    for m in messages:
        if m.photo:
            photo = m.photo[-1]  # Highest resolution / Максимальное разрешение
            filename = f"photo_{m.message_id}.jpg"
            path = await download_tg_file(photo, context, filename)
            if path:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                photos_html += f'<p><img src="data:image/jpeg;base64,{b64}" /></p>'
                os.remove(path)

    html_content = text_to_html(text) if text else ""
    html_content += photos_html

    lines = text.strip().splitlines() if text.strip() else []
    title = lines[0][:100] if lines else f"Фото {messages[0].date.strftime('%d.%m.%Y %H:%M')}"

    # Apply pending tags or extract from text
    # Применяем отложенные теги или извлекаем из текста
    global pending_tags
    if pending_tags:
        tags = pending_tags
        pending_tags = []
        logger.info(f"Применяем отложенные теги к альбому / applying pending tags to album: {tags}")
    else:
        tags = extract_tags(text)

    object_id, ver = create_note(title, html_content)
    reply_msg = messages[-1]
    if object_id:
        if tags:
            set_note_tags(object_id, ver, tags)
        await reply_msg.reply_text(f"✅ Сохранено: «{title}»")
    else:
        await reply_msg.reply_text("❌ Не удалось сохранить заметку.")


# ══════════════════════════════════════════════
# Command handlers / Обработчики команд
# ══════════════════════════════════════════════

async def handle_tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /tag command — remember tags for the next message.
    Команда /tag — запомнить теги для следующего сообщения.
    Usage / Использование: /tag #urgent #work
    """
    global pending_tags
    msg = update.message
    if not msg:
        return

    if msg.from_user.id != ALLOWED_USER_ID:
        await msg.reply_text("⛔ Нет доступа. / Access denied.")
        return

    text = msg.text or ""
    tags = re.findall(r'#(\S+)', text)

    if not tags:
        await msg.reply_text("❌ Укажи теги: /tag #срочно #работа\nExample: /tag #urgent #work")
        return

    pending_tags = tags
    tags_str = " ".join(f"#{t}" for t in tags)
    await msg.reply_text(
        f"🏷 Теги запомнены / Tags saved: {tags_str}\n"
        f"Отправь следующий пост — он будет сохранён с этими тегами.\n"
        f"Send the next message — it will be saved with these tags."
    )


# ══════════════════════════════════════════════
# Message handler / Обработчик сообщений
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main message handler — processes text, photos and files.
    Основной обработчик сообщений — обрабатывает текст, фото и файлы.
    """
    msg = update.message
    if not msg:
        return

    # Access check / Проверка доступа
    if msg.from_user.id != ALLOWED_USER_ID:
        await msg.reply_text("⛔ Нет доступа. / Access denied.")
        return

    # Album (multiple photos) — collect and process together
    # Альбом (несколько фото) — собираем и обрабатываем вместе
    if msg.media_group_id:
        group_id = msg.media_group_id
        if group_id not in album_groups:
            album_groups[group_id] = []
        album_groups[group_id].append(msg)
        if group_id in album_timers:
            album_timers[group_id].cancel()
        album_timers[group_id] = asyncio.create_task(process_album(group_id, context))
        return

    text = msg.text or msg.caption or ""
    attachment_ids = []
    extra_html = ""

    # ── Photo / Фото ───────────────────────────
    if msg.photo:
        photo = msg.photo[-1]  # Highest resolution / Максимальное разрешение
        filename = f"photo_{msg.message_id}.jpg"
        path = await download_tg_file(photo, context, filename)
        if path:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            extra_html += f'<p><img src="data:image/jpeg;base64,{b64}" /></p>'
            os.remove(path)

    # ── Document / file — Документ / файл ──────
    doc_path = None
    doc_filename = None
    if msg.document:
        import mimetypes
        doc = msg.document
        doc_filename = doc.file_name or f"file_{msg.message_id}"
        doc_path = await download_tg_file(doc, context, doc_filename)
        if doc_path:
            mime = mimetypes.guess_type(doc_filename)[0] or "application/octet-stream"
            if mime.startswith("image/"):
                # Image files — embed as base64 / Картинки — вставляем как base64
                with open(doc_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                extra_html += f'<p><img src="data:{mime};base64,{b64}" /></p>'
                os.remove(doc_path)
                doc_path = None  # Already handled / Уже обработан

    # ── Build title / Формируем заголовок ──────
    lines = text.strip().splitlines() if text.strip() else []
    if lines:
        title = lines[0][:100]
    elif msg.photo:
        title = f"Фото {msg.date.strftime('%d.%m.%Y %H:%M')}"
    elif msg.document:
        title = msg.document.file_name or f"Файл {msg.date.strftime('%d.%m.%Y %H:%M')}"
    else:
        title = f"Заметка {msg.date.strftime('%d.%m.%Y %H:%M')}"

    # ── Build HTML content / Собираем HTML ─────
    html_content = text_to_html(text) if text else ""
    html_content += extra_html

    # Apply pending tags or extract from text
    # Применяем отложенные теги или извлекаем из текста
    global pending_tags
    if pending_tags:
        tags = pending_tags
        pending_tags = []
        logger.info(f"Применяем отложенные теги / applying pending tags: {tags}")
    else:
        tags = extract_tags(text)

    object_id, ver = create_note(title, html_content)

    if object_id:
        if tags:
            set_note_tags(object_id, ver, tags)
        # Attach non-image file if present / Прикрепляем файл если есть
        if doc_path and doc_filename:
            attach_file_to_note(object_id, ver, doc_path, doc_filename)
            os.remove(doc_path)
        await msg.reply_text(f"✅ Сохранено: «{title}»")
    else:
        if doc_path:
            os.remove(doc_path)
        await msg.reply_text("❌ Не удалось сохранить заметку. / Failed to save note.")


# ══════════════════════════════════════════════
# Entry point / Точка входа
# ══════════════════════════════════════════════

def main():
    if not syno_login():
        logger.error("Не удалось подключиться к Synology. / Failed to connect to Synology.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("tag", handle_tag_command))
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.Document.ALL,
        handle_message
    ))

    logger.info("Бот запущен. Ожидаю сообщения... / Bot started. Waiting for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
