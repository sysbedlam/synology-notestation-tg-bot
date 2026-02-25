# 🤖 Synology Note Station — Telegram Bot

> Save Telegram messages directly to Synology Note Station  
> Сохраняйте сообщения из Telegram прямо в Synology Note Station

---

## Features / Возможности

| Feature | Функция |
|---|---|
| 📝 Text → note (first line = title) | Текст → заметка (первая строка = заголовок) |
| 🖼 Photo → note with image | Фото → заметка с изображением |
| 🖼🖼 Album → single note with all photos | Альбом → одна заметка со всеми фото |
| 📎 Files → attached to note | Файлы → вложения к заметке |
| 🔗 Links become clickable | Ссылки становятся кликабельными |
| 🏷 Tags via `/tag` or hashtags | Теги через `/tag` или хэштеги |
| ⛔ Single-user access by Telegram ID | Доступ только для одного пользователя |

---

## Requirements / Требования

- Synology NAS with DSM 7.x
- [Note Station](https://www.synology.com/en-global/dsm/feature/note_station) installed
- [Synology Drive](https://www.synology.com/en-global/dsm/feature/drive) installed and initialized
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

---

## Installation / Установка

### 🐳 Docker (recommended / рекомендуется)

**EN:** No SSH required. Uses Synology Container Manager.  
**RU:** SSH не нужен. Используется Container Manager.

1. Install **Container Manager** from Package Center
2. Upload these files to one folder (e.g. `/volume1/docker/note_bot/`):
   - `bot.py`
   - `requirements.txt`
   - `docker-compose.yml`
   - `.env` (copy from `.env.example` and fill in)
3. Container Manager → **Project** → **Create** → select the folder
4. Done! Check logs in Container Manager → Project → Log

**.env example:**
```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
SYNOLOGY_HOST=https://192.168.1.100:5001
SYNOLOGY_USER=your_username
SYNOLOGY_PASS=your_password
NOTE_NOTEBOOK=Telegram
ALLOWED_USER_ID=123456789
```

---

### 🖥 Standalone (without Docker / без Docker)

**EN:** Requires SSH and Python 3 from Package Center.  
**RU:** Требует SSH и Python 3 из Package Center.

1. Install **Python 3** from Package Center
2. Upload `bot.py` and `requirements.txt` to `/volume1/tg_bots/note_bot/`
3. Fill in settings at the top of `bot.py`
4. Install dependencies:
```bash
pip3 install -r requirements.txt
```
5. Run:
```bash
python3 /volume1/tg_bots/note_bot/bot.py
```
6. Auto-start: DSM → Task Scheduler → Triggered Task → Boot-up:
```bash
python3 /volume1/tg_bots/note_bot/bot.py >> /volume1/tg_bots/note_bot/bot.log 2>&1 &
```

---

## Usage / Использование

### Tags / Теги

**Via command / Через команду:**
```
/tag #urgent #work
```
Bot remembers tags and applies them to the next message, then resets.  
Бот запомнит теги и применит к следующему сообщению, затем сбросит.

**Via text / Через текст:**  
If the **last line** consists only of hashtags — they become tags.  
Если **последняя строка** состоит только из хэштегов — они станут тегами.
```
Note text here
#idea #readlater
```

### Album / Альбом
Send multiple photos at once — bot combines them into one note.  
Отправь несколько фото сразу — бот объединит их в одну заметку.

### Files / Файлы
Send any file — it will be attached to the note. Telegram limit: 20 MB.  
Отправь любой файл — прикрепится как вложение. Лимит Telegram: 20 МБ.

---

## Register bot commands / Регистрация команд бота

[@BotFather](https://t.me/BotFather) → `/setcommands` → select your bot → send:
```
tag - Set tags for the next message
```

---

## Notes / Примечания

- Both **Note Station** and **Synology Drive** must be installed and initialized
- Standalone mode requires running as `root` (Synology limitation)
- `verify=False` is used for SSL — safe for local network usage
- Tested on DSM 7.x

---

## License / Лицензия

MIT — free to use, modify and distribute.  
MIT — можно использовать, изменять и распространять свободно.

---

*Built with the help of [Claude](https://claude.ai) by Anthropic*
