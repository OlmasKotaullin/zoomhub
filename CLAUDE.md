# CLAUDE.md — ZoomHub

## Обязательное тестирование

После завершения КАЖДОЙ задачи (фича, баг-фикс, рефакторинг) — запусти тесты:

```bash
cd /Users/angel/Вайбкодинг\ 2025/projects/zoomhub

# 1. Синтаксис Python — все изменённые файлы
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['ФАЙЛЫ']]; print('OK')"

# 2. Jinja2 шаблоны — баланс тегов
python3 -c "
import re
for f in ['ШАБЛОНЫ']:
    with open(f) as fh: c = fh.read()
    ifs = len(re.findall(r'\{% *if ', c)); endifs = len(re.findall(r'\{% *endif', c))
    fors = len(re.findall(r'\{% *for ', c)); endfors = len(re.findall(r'\{% *endfor', c))
    ok = ifs == endifs and fors == endfors
    print(f\"{'OK' if ok else 'ERROR'}: {f}\")
"

# 3. Продакшен тесты — API endpoints на Fly.io
python3 -c "
import requests, json
BASE = 'https://zoomhub-app.fly.dev'
s = requests.Session()
r = s.post(f'{BASE}/api/auth/register', json={'name':'Test','email':'autotest@t.com','password':'test123456'})
if r.status_code == 409:
    r = s.post(f'{BASE}/api/auth/login', json={'email':'autotest@t.com','password':'test123456'})
s.cookies.set('session_token', r.json().get('token',''))
# ... тесты эндпоинтов ...
"
```

**Правила:**
- НЕ говори что всё работает, пока не прогнал тесты на проде
- Тестируй минимум 5 запросов подряд для streaming (rate limits)
- Проверяй onclick в шаблонах — `|tojson` + одинарные кавычки `onclick='...'`
- При 429 ошибках — проверяй fallback цепочку (Groq → Gemini → Claude)
- После деплоя жди 10 секунд и тестируй на `https://zoomhub-app.fly.dev`

## Стек

- **Backend:** FastAPI + PostgreSQL + SQLAlchemy на Fly.io (Amsterdam)
- **Frontend:** Jinja2 + HTMX + Tailwind CDN + vanilla JS
- **LLM:** Groq (Llama 3.3 70B) → Gemini 2.5 Flash → Claude Sonnet 4 (fallback chain)
- **Транскрипция:** Буквица (Telegram-бот)
- **Деплой:** `fly deploy --depot=false`

## Ключевые файлы

| Файл | Описание |
|---|---|
| `app/main.py` | FastAPI app, middleware, lifespan |
| `app/models.py` | SQLAlchemy модели |
| `app/database.py` | Engine, sessions, auto-migration |
| `app/routers/chat.py` | AI-чат: streaming SSE, шаблоны, /chat страница |
| `app/routers/meetings.py` | Встречи: CRUD, upload, search |
| `app/routers/folders.py` | Папки, настройки |
| `app/services/chat_engine.py` | Контекст встречи/папки для LLM |
| `app/services/pipeline.py` | Транскрипция → саммари pipeline |
| `app/services/providers/` | LLM провайдеры (groq, gemini, claude, ollama) |
| `app/templates/chat.html` | Страница AI-чата |
| `app/templates/meeting.html` | Страница встречи |
| `app/templates/base.html` | Layout + sidebar |
| `agent/zoomhub_agent_v2.py` | Локальный агент (мониторинг Zoom папки) |

## Известные проблемы

- Groq и Gemini бесплатные — rate limits при быстрых запросах. Fallback на Claude.
- Telegram-сессия на сервере может протухнуть — нужно перезалить: `fly ssh sftp shell` → `put ~/.zoomhub/zoomhub.session /app/data/zoomhub.session`
- `|tojson` в Jinja2 генерирует двойные кавычки — onclick атрибуты ТОЛЬКО через одинарные: `onclick='fn({{ val|tojson }})'`
