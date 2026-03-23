# ZoomHub Auto-Capture — автоматическое получение записей

## Overview

Система автоматического захвата Zoom-записей из двух источников: Zoom Cloud API (облачные записи) и локальный агент (записи на компьютере пользователя). Пользователь выбирает источник в настройках. После обработки — уведомление в Telegram и email.

## Requirements

### Functional Requirements

1. **Zoom Cloud API** — пользователь подключает свой Zoom-аккаунт через OAuth-кнопку «Подключить Zoom» в настройках ZoomHub. После этого сервер автоматически каждые 2 минуты проверяет наличие новых записей и скачивает их.
2. **Локальный агент** — лёгкий Python-скрипт, который пользователь запускает на своём компьютере (Mac/Windows). Сканирует папку Zoom (настраиваемый путь), при появлении нового файла загружает его на ZoomHub через API.
3. **Настраиваемый источник** — в настройках ZoomHub пользователь выбирает: «Zoom Cloud», «Локальный агент», или «Оба».
4. **Уведомления** — после обработки записи пользователь получает уведомление с кратким саммари:
   - Telegram-бот (пользователь привязывает свой Telegram в настройках)
   - Email (на адрес регистрации)
   - Веб-UI (всегда)
5. **Дедупликация** — если запись пришла и из Cloud API, и из агента, обрабатывается только один раз.
6. **Автоматическая обработка** — pipeline: скачивание → транскрипция (Буквица) → саммари (Claude) → уведомление.

### Non-Functional Requirements

- Агент должен работать на Mac и Windows без установки Python (скомпилированный бинарник или PyInstaller)
- Zoom OAuth должен быть прозрачным для пользователя — одна кнопка
- Уведомления должны приходить в течение 5 минут после окончания встречи
- Агент потребляет минимум ресурсов (проверка раз в 30 секунд)

## Architecture

### High-Level Design

```
┌─────────────────────────────────────────────┐
│         ZoomHub Server (Fly.io)            │
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Zoom     │  │ Upload   │  │ Notify   │ │
│  │ Poller   │  │ API      │  │ Service  │ │
│  │ (Cloud)  │  │ (Agent)  │  │ TG+Email │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘ │
│       │              │              │       │
│       └──────┬───────┘              │       │
│              ▼                      │       │
│       ┌──────────┐                  │       │
│       │ Pipeline │──────────────────┘       │
│       │ Transcr. │                          │
│       │ Summary  │                          │
│       └──────────┘                          │
└─────────────────────────────────────────────┘
        ▲                    ▲
        │                    │
   Zoom Cloud API    Локальный агент
   (OAuth per user)  (Python скрипт)
        ▲                    ▲
        │                    │
   Zoom аккаунт      Папка ~/Documents/Zoom
   пользователя      на компе пользователя
```

### Technology Stack

- **Zoom OAuth**: User-level OAuth 2.0 (не Server-to-Server — каждый пользователь свой)
- **Локальный агент**: Python + `watchdog` (file system watcher) + `httpx` (API client)
- **Уведомления**: `aiogram` (Telegram Bot API) + `aiosmtplib` (email)
- **Всё остальное**: существующий стек (FastAPI, PostgreSQL, Буквица, Claude)

### Component Overview

| Компонент | Ответственность |
|-----------|----------------|
| `app/services/zoom_oauth.py` | User-level Zoom OAuth flow |
| `app/services/zoom_user_poller.py` | Polling Zoom API per user |
| `app/services/notify.py` | Telegram + Email уведомления |
| `agent/zoomhub-agent.py` | Локальный скрипт-агент |
| `app/routers/zoom.py` (расширить) | OAuth callback, webhook |

## Data Model

### Новые/изменённые сущности

**User (расширить)**:
- `zoom_access_token` — OAuth access token
- `zoom_refresh_token` — OAuth refresh token
- `zoom_token_expires_at` — когда истечёт
- `zoom_user_email` — Zoom email
- `telegram_chat_id` — для уведомлений в Telegram
- `notify_telegram` — bool, включены ли TG уведомления
- `notify_email` — bool, включены ли email уведомления
- `capture_source` — enum: cloud | agent | both

**Meeting (расширить)**:
- `source_type` — enum: cloud_api | agent_upload | manual_upload
- `zoom_recording_id` — для дедупликации (unique)

### Storage

PostgreSQL (уже настроен на Fly.io). Zoom-токены хранятся зашифрованными в БД.

## API Design

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/auth/zoom` | Начать Zoom OAuth | Session |
| GET | `/auth/zoom/callback` | Zoom OAuth callback | — |
| POST | `/api/zoom/disconnect` | Отключить Zoom | Session/JWT |
| GET | `/api/zoom/status` | Статус подключения Zoom | Session/JWT |
| POST | `/api/agent/upload` | Агент загружает файл | JWT (API token) |
| GET | `/api/agent/token` | Получить API token для агента | Session |
| POST | `/settings/notifications` | Настроить уведомления | Session |
| POST | `/settings/capture-source` | Выбрать источник записей | Session |

## Authentication & Authorization

- **Zoom OAuth**: User-level OAuth 2.0, scopes: `recording:read`, `user:read:email`
- **Локальный агент**: Использует API token пользователя (генерируется в настройках ZoomHub, JWT без срока годности)
- **Telegram**: Пользователь нажимает «Подключить Telegram» → получает ссылку на бота → отправляет /start → бот привязывает `chat_id` к аккаунту

## Уведомления

### Telegram
- Бот `@ZoomHubBot` отправляет сообщение после обработки:
  ```
  📋 Встреча обработана: "Планёрка с командой"

  TLDR: Обсудили дедлайны Q2, решили перенести запуск на 15 апреля.

  📝 Задачи:
  • Подготовить презентацию — Алмаз, до 10.04
  • Обновить roadmap — Марат, до 12.04

  🔗 Подробнее: https://zoomhub-app.fly.dev/meetings/42
  ```

### Email
- Аналогичное содержание в HTML-формате на email регистрации

## Локальный агент

### Установка и запуск

```bash
# Пользователь скачивает с ZoomHub
# Вариант 1: Python
pip install zoomhub-agent
zoomhub-agent --token YOUR_API_TOKEN --server https://zoomhub-app.fly.dev

# Вариант 2: Скомпилированный (PyInstaller)
# Скачать zoomhub-agent.exe (Windows) или zoomhub-agent (Mac)
./zoomhub-agent --token YOUR_API_TOKEN
```

### Логика работы

1. При запуске спрашивает путь к папке Zoom (default: `~/Documents/Zoom` на Mac, `%USERPROFILE%\Documents\Zoom` на Windows)
2. Сканирует папку каждые 30 секунд
3. При появлении нового .mp4/.m4a файла:
   - Ждёт 10 секунд (файл может ещё записываться)
   - Проверяет что файл не менялся последние 10 секунд
   - Загружает через `POST /api/agent/upload`
   - Помечает как загруженный (локальный JSON-файл с хешами)
4. Работает в фоне, минимальное потребление ресурсов

## Testing Plan

### Unit Tests
- Zoom OAuth flow (mock Zoom API)
- Agent upload endpoint
- Дедупликация записей
- Notification formatting

### Integration Tests
- Полный pipeline: upload → transcribe → summarize → notify
- Zoom token refresh
- Agent file detection + upload

### End-to-End Tests
- Пользователь подключает Zoom → запись появляется автоматически
- Агент обнаруживает файл → загружает → уведомление в Telegram

## Deployment

- **Сервер**: существующий Fly.io (zoomhub-app)
- **Telegram Bot**: создать через @BotFather, добавить `TELEGRAM_BOT_TOKEN` в secrets
- **Zoom OAuth App**: создать на marketplace.zoom.us (User-level OAuth, не Server-to-Server)
- **Agent**: опубликовать на PyPI или как бинарник в GitHub Releases

## Open Questions

- Лимиты Zoom API для бесплатных аккаунтов (rate limits на polling)
- Максимальный размер файла для загрузки через агента (сейчас 2 ГБ в конфиге, Fly.io может ограничивать)
- Нужен ли webhook от Zoom вместо polling (быстрее, но сложнее настроить)
- Шифрование Zoom-токенов в БД — нужна ли отдельная encryption key
