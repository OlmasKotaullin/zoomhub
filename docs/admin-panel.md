# Админ-панель, тикет-система, сброс пароля — ZoomHub

## Overview

ZoomHub готовится к запуску тестовой группы (7-10 маркетологов/предпринимателей). Владельцу нужна админ-панель для мониторинга активности пользователей и решения их проблем. Клиентам нужна возможность восстановить пароль и сообщить о проблемах через тикет-систему.

## Requirements

### Functional Requirements

1. **Админ-панель** — доступна только пользователям с `is_admin=True`
   - Dashboard со сводной статистикой: всего пользователей, активных, встреч обработано, часов аудио
   - Таблица пользователей: имя, email, дата регистрации, кол-во встреч, часы аудио, дата последней встречи, LLM-провайдер, источник (агент/вручную), конверсия (загрузил → получил конспект), статус
   - График активности по дням (последние 30 дней)
   - Топ пользователей по кол-ву встреч
   - Средний размер встречи (слов/минут)
   - Деактивация/активация пользователей
   - Управление инвайт-кодами (список, создание новых)

2. **Тикет-система** — полноценные обращения со статусами
   - Клиент создаёт тикет: тема + описание проблемы
   - Статусы: новый → в работе → решён
   - Приоритеты: обычный / важный / критический
   - Категории: баг / вопрос / предложение
   - Админ видит все тикеты, может менять статус, отвечать
   - Клиент видит свои тикеты и ответы админа
   - Бейдж с числом непрочитанных тикетов в sidebar админа

3. **Восстановление пароля**
   - Ссылка «Забыли пароль?» на странице логина
   - Ввод email → генерация токена (1 час) → отправка письма через Yandex SMTP
   - По ссылке из письма — форма нового пароля
   - Защита от перечисления email: всегда показывает «письмо отправлено»

### Non-Functional Requirements

- Безопасность: /admin защищён проверкой is_admin, /forgot-password не раскрывает email
- Производительность: статистика рассчитывается SQL-запросами, не в Python
- UX: единый стиль с существующим интерфейсом (z-card, z-btn, Inter, tailwind)
- Без новых зависимостей: bcrypt, aiosmtplib, secrets — всё уже есть

## Architecture

### Technology Stack

- Backend: FastAPI + SQLAlchemy (существующий стек)
- Frontend: Jinja2 + HTMX + Tailwind CSS (существующий стек)
- Email: aiosmtplib через Yandex SMTP (smtp.yandex.ru:465, SSL)
- Database: PostgreSQL (prod) / SQLite (dev)

### Component Overview

| Компонент | Ответственность |
|-----------|----------------|
| `app/routers/admin.py` | Все admin-маршруты: dashboard, users, tickets, invites |
| `app/routers/auth.py` | +4 маршрута для forgot/reset password |
| `app/templates/admin.html` | Единый шаблон админки с табами |
| `app/templates/support.html` | Клиентская страница тикетов |
| `app/templates/forgot_password.html` | Форма ввода email |
| `app/templates/reset_password.html` | Форма нового пароля |

## Data Model

### Entities

**User** (существующая, расширение):
```
+ is_admin: Boolean (default=False)
```

**SupportTicket** (новая):
```
id: Integer PK
user_id: Integer FK→users.id
subject: String(500) — тема обращения
message: Text — описание проблемы
category: String(50) — "bug" | "question" | "suggestion"
priority: String(50) — "normal" | "important" | "critical"
status: String(50) — "new" | "in_progress" | "resolved"
is_read: Boolean (default=False)
admin_reply: Text (nullable) — ответ админа
created_at: DateTime
updated_at: DateTime
```

**PasswordReset** (новая):
```
id: Integer PK
user_id: Integer FK→users.id
token: String(255) unique, indexed
used: Boolean (default=False)
created_at: DateTime
expires_at: DateTime (created_at + 1 hour)
```

### Relationships

- SupportTicket.user → User (many-to-one)
- PasswordReset.user → User (many-to-one)

### Storage

PostgreSQL (prod), SQLite (dev). Auto-migration в `database.py:init_db()`.

## API Design

### Админ-панель

| Method | Path | Description |
|--------|------|-------------|
| GET | /admin | Dashboard — сводная статистика, график |
| GET | /admin/users | Таблица пользователей с метриками |
| POST | /admin/users/{id}/toggle | Активировать/деактивировать пользователя (HTMX) |
| GET | /admin/tickets | Все тикеты |
| POST | /admin/tickets/{id}/status | Изменить статус тикета (HTMX) |
| POST | /admin/tickets/{id}/reply | Ответить на тикет (HTMX) |
| GET | /admin/invites | Список инвайт-кодов |
| POST | /admin/invites | Создать инвайт-код |

### Тикет-система (клиент)

| Method | Path | Description |
|--------|------|-------------|
| GET | /support | Страница тикетов пользователя |
| POST | /support | Создать новый тикет |

### Сброс пароля

| Method | Path | Description |
|--------|------|-------------|
| GET | /forgot-password | Форма ввода email |
| POST | /forgot-password | Генерация токена + отправка email |
| GET | /reset-password?token=xxx | Форма нового пароля |
| POST | /reset-password | Сохранение нового пароля |

## Authentication & Authorization

- **Админ-маршруты**: `require_admin(request, db)` — проверяет `user.is_admin == True`, иначе redirect `/`
- **Тикеты**: авторизованные пользователи видят только свои тикеты
- **Сброс пароля**: публичные маршруты (`_PUBLIC_PREFIXES`)
- Токен сброса: одноразовый, истекает через 1 час

## Error Handling

- Сброс пароля: не раскрывает существование email (всегда «письмо отправлено»)
- SMTP не настроен: показывает «Сервис отправки почты временно недоступен»
- Невалидный/истёкший токен: «Ссылка недействительна или устарела»
- Нет доступа к админке: redirect на главную

## Testing Plan

### End-to-End Tests

1. Деплой → миграция создаёт is_admin, таблицы support_tickets и password_resets
2. `UPDATE users SET is_admin = true WHERE email = 'annggeellooss@gmail.com'` (через миграцию)
3. Открыть /admin — доступен для админа, недоступен для обычного пользователя
4. Создать тикет через /support → увидеть в /admin/tickets
5. Ответить на тикет → клиент видит ответ
6. Изменить статус тикета → бейдж обновляется
7. /forgot-password → email → ссылка → новый пароль → /login с новым паролем
8. Статистика пользователей корректна
9. Создать/деактивировать инвайт-код
10. Все формы без 422 ошибок

## Deployment

- **SMTP secrets**: `fly secrets set SMTP_HOST=smtp.yandex.ru SMTP_PORT=465 SMTP_USER=... SMTP_PASSWORD=...`
- Деплой: `fly deploy` (remote builder)
- Миграция: автоматическая через `init_db()` при старте
- Первый админ: автоматически назначается через миграцию по email `annggeellooss@gmail.com`

## Open Questions

- Нужна ли отправка уведомления клиенту при ответе на тикет (email/Telegram)?
- Нужно ли хранить историю статусов тикета (кто когда менял)?
- Нужна ли пагинация в таблице пользователей (для >50 юзеров)?
