import logging
import os
import asyncio
import base64
import json
import requests
import urllib3
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
# НАСТРОЙКИ — заполни перед запуском
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "11111111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

SYNOLOGY_HOST = "https://localhost:5001"   # на NAS можно использовать localhost
SYNOLOGY_USER = "user"
SYNOLOGY_PASS = "password"
NOTE_NOTEBOOK = "Telegram"               # название блокнота в Note Station

ALLOWED_USER_ID = 11111111111111              # твой Telegram ID (узнай у @userinfobot)

TEMP_DIR = "/tmp/tg_bot_files"           # временная папка для скачивания файлов
# Задержка сбора альбома (сек)
ALBUM_COLLECT_DELAY = 2.0
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

os.makedirs(TEMP_DIR, exist_ok=True)

session = requests.Session()
syno_sid = None

# Хранилище для группировки альбомов
album_groups = {}
album_timers = {}

# Теги ожидающие следующего сообщения
pending_tags = []


# ══════════════════════════════════════════════
# Synology API
# ══════════════════════════════════════════════

def syno_login() -> bool:
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
            logger.info("Synology: авторизация успешна")
            return True
        logger.error(f"Synology: ошибка авторизации {data}")
    except Exception as e:
        logger.error(f"Synology: ошибка подключения {e}")
    return False


def ensure_auth() -> bool:
    global syno_sid
    if syno_sid:
        return True
    return syno_login()


def get_or_create_notebook(name: str) -> object:
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

        # Создаём блокнот если не нашли
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
    """Прикрепляет файл к существующей заметке через multipart запрос."""
    import mimetypes
    # api/version/method/_sid идут в URL, остальное в multipart body
    url = f"{SYNOLOGY_HOST}/webapi/entry.cgi?api=SYNO.NoteStation.Note&version=3&method=set&_sid={syno_sid}"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    import random
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
            "object_id": f'"{object_id}"',
            "ver": f'"{ver}"',
            "commit_msg": json.dumps({"device": "tgbot"}),
            "attachment": attachment_meta,
        }
        multipart = {
            field_name: (filename, file_data, mime),
        }
        r = session.post(url, data=data_fields, files=multipart, verify=False, timeout=60)
        data = r.json()
        if data.get("success"):
            logger.info(f"Файл прикреплён: {filename}")
            return True
        logger.error(f"Ошибка прикрепления файла: {data}")
    except Exception as e:
        logger.error(f"attach_file_to_note: {e}")
    return False


def create_note(title: str, html_content: str, attachment_ids: list = None):
    """Создаёт заметку. Возвращает (object_id, ver) или (None, None)."""
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
            logger.info(f"Заметка создана: {title}")
            return data["data"]["object_id"], data["data"]["ver"]
        # Истёкшая сессия — переавторизуемся
        if data.get("error", {}).get("code") in (105, 106, 119):
            syno_login()
            payload["_sid"] = syno_sid
            r = session.post(url, data=payload, verify=False, timeout=15)
            data = r.json()
            if data.get("success"):
                return data["data"]["object_id"], data["data"]["ver"]
        logger.error(f"Ошибка создания заметки: {data}")
    except Exception as e:
        logger.error(f"create_note: {e}")
    return None, None


def set_note_tags(object_id: str, ver: str, tags: list) -> bool:
    """Устанавливает теги для существующей заметки."""
    if not tags:
        return True
    url = f"{SYNOLOGY_HOST}/webapi/entry.cgi?api=SYNO.NoteStation.Note&version=3&method=set&_sid={syno_sid}"
    payload = {
        "object_id": f'"{object_id}"',
        "ver": f'"{ver}"',
        "tag": json.dumps(tags),
        "commit_msg": '{"device":"tgbot"}',
        "check_conflict": "false",
    }
    try:
        r = session.post(url, data=payload, verify=False, timeout=15)
        data = r.json()
        if data.get("success"):
            logger.info(f"Теги установлены: {tags}")
            return True
        logger.error(f"Ошибка установки тегов: {data}")
    except Exception as e:
        logger.error(f"set_note_tags: {e}")
    return False


# ══════════════════════════════════════════════
# Утилиты
# ══════════════════════════════════════════════

def text_to_html(text: str) -> str:
    import html
    import re
    escaped = html.escape(text)
    # Делаем ссылки кликабельными
    escaped = re.sub(r'(https?://\S+)', r'<a href="\1">\1</a>', escaped)
    return "<p>" + escaped.replace("\n", "</p><p>") + "</p>"


async def download_tg_file(file_obj, context, filename: str) -> object:
    """Скачивает файл из Telegram во временную директорию."""
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        path = os.path.join(TEMP_DIR, filename)
        await tg_file.download_to_drive(path)
        return path
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
    return None


import re

def extract_tags(text: str) -> list:
    """Извлекает хэштеги из последней строки текста."""
    if not text.strip():
        return []
    lines = text.strip().splitlines()
    last_line = lines[-1].strip()
    # Только если последняя строка состоит исключительно из хэштегов
    if re.match(r'^(#\S+\s*)+$', last_line):
        return [tag.lstrip('#') for tag in last_line.split()]
    return []


async def process_album(group_id: str, context):
    """Обрабатывает все фото из одного альбома как одну заметку."""
    await asyncio.sleep(ALBUM_COLLECT_DELAY)

    messages = album_groups.pop(group_id, [])
    album_timers.pop(group_id, None)

    if not messages:
        return

    text = ""
    for m in messages:
        if m.caption:
            text = m.caption
            break

    photos_html = ""
    for m in messages:
        if m.photo:
            photo = m.photo[-1]
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
    global pending_tags
    if pending_tags:
        tags = pending_tags
        pending_tags = []
        logger.info(f"Применяем отложенные теги к альбому: {tags}")
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



async def handle_tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_tags
    msg = update.message
    if not msg:
        return

    if msg.from_user.id != ALLOWED_USER_ID:
        await msg.reply_text("⛔ Нет доступа.")
        return

    text = msg.text or ""
    # Извлекаем хэштеги из команды
    import re
    tags = re.findall(r'#(\S+)', text)

    if not tags:
        await msg.reply_text("❌ Укажи теги: /tag #срочно #работа")
        return

    pending_tags = tags
    tags_str = " ".join(f"#{t}" for t in tags)
    await msg.reply_text(f"🏷 Теги запомнены: {tags_str}\nОтправь следующий пост — он будет сохранён с этими тегами.")


# ══════════════════════════════════════════════
# Обработчик сообщений
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # Проверка — только разрешённый пользователь
    if msg.from_user.id != ALLOWED_USER_ID:
        await msg.reply_text("⛔ Нет доступа.")
        return

    # ── Альбом (несколько фото) ────────────────
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

    # ── Фото ──────────────────────────────────
    if msg.photo:
        import base64
        photo = msg.photo[-1]  # максимальное разрешение
        filename = f"photo_{msg.message_id}.jpg"
        path = await download_tg_file(photo, context, filename)
        if path:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            extra_html += f'<p><img src="data:image/jpeg;base64,{b64}" /></p>'
            os.remove(path)

    # ── Документ / файл ───────────────────────
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
                # Картинки всё равно вставляем как base64
                with open(doc_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                extra_html += f'<p><img src="data:{mime};base64,{b64}" /></p>'
                os.remove(doc_path)
                doc_path = None  # файл уже обработан

    # ── Формируем заголовок ───────────────────
    lines = text.strip().splitlines() if text.strip() else []
    if lines:
        title = lines[0][:100]
    elif msg.photo:
        title = f"Фото {msg.date.strftime('%d.%m.%Y %H:%M')}"
    elif msg.document:
        title = msg.document.file_name or f"Файл {msg.date.strftime('%d.%m.%Y %H:%M')}"
    else:
        title = f"Заметка {msg.date.strftime('%d.%m.%Y %H:%M')}"

    # ── Собираем HTML контент ─────────────────
    html_content = text_to_html(text) if text else ""
    html_content += extra_html

    # Берём pending_tags если есть, иначе извлекаем из текста
    global pending_tags
    if pending_tags:
        tags = pending_tags
        pending_tags = []
        logger.info(f"Применяем отложенные теги: {tags}")
    else:
        tags = extract_tags(text)

    object_id, ver = create_note(title, html_content)

    if object_id:
        if tags:
            set_note_tags(object_id, ver, tags)
        # Прикрепляем файл если есть
        if doc_path and doc_filename:
            attach_file_to_note(object_id, ver, doc_path, doc_filename)
            os.remove(doc_path)
        await msg.reply_text(f"✅ Сохранено: «{title}»")
    else:
        if doc_path:
            os.remove(doc_path)
        await msg.reply_text("❌ Не удалось сохранить заметку. Проверь логи.")


# ══════════════════════════════════════════════
# Запуск
# ══════════════════════════════════════════════

def main():
    if not syno_login():
        logger.error("Не удалось подключиться к Synology. Проверь настройки.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("tag", handle_tag_command))
    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.Document.ALL,
        handle_message
    ))

    logger.info("Бот запущен. Ожидаю сообщения...")
    app.run_polling()


if __name__ == "__main__":
    main()
