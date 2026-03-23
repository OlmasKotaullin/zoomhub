# ZoomHub — Умная маршрутизация LLM

## Overview
Автоматический выбор LLM-провайдера (Ollama локально / Claude API) в зависимости от длины транскрипта встречи. Короткие встречи обрабатываются бесплатно через Qwen 2.5:7b, длинные — через Claude для полноты контекста. Пользователь может переключить режим в настройках.

## Requirements

### Functional Requirements
1. Три режима LLM: `auto` (по умолчанию), `ollama` (только локально), `claude` (только API)
2. В режиме `auto`: транскрипт <10K символов → Ollama, ≥10K → Claude
3. Модель Ollama: qwen2.5:7b (16 ГБ RAM, ~5 ГБ диска)
4. UI настроек показывает текущий режим и позволяет переключать
5. При каждом саммари в лог пишется какой провайдер был использован
6. Чат использует тот же провайдер, что и саммари (или auto по длине контекста)

### Non-Functional Requirements
- Ollama qwen2.5:7b загружается ~30 сек при первом запросе, потом в кэше
- Claude API latency ~3-5 сек
- Переключение режима — мгновенное, без перезапуска

## Architecture

### Логика маршрутизации (summarizer.py)
```python
def get_provider_for_text(text_length: int) -> LLMProvider:
    if LLM_PROVIDER == "ollama":
        return OllamaProvider()
    elif LLM_PROVIDER == "claude":
        return ClaudeProvider()
    else:  # "auto"
        if text_length < 10_000:  # ~3K токенов
            return OllamaProvider()
        else:
            if ANTHROPIC_API_KEY:
                return ClaudeProvider()
            else:
                return OllamaProvider()  # fallback если нет ключа
```

### Контекстные лимиты
| Провайдер | Модель | Контекст | Лимит транскрипта |
|-----------|--------|----------|-------------------|
| Ollama | qwen2.5:7b | 32K токенов | 15K символов |
| Claude | claude-sonnet-4 | 200K токенов | 100K символов |

### Technology Stack
- Ollama + qwen2.5:7b (локально, бесплатно)
- Claude API claude-sonnet-4 (платно, для длинных)
- Python FastAPI (бэкенд)
- SwiftUI (нативное приложение)

## Изменяемые файлы

| Файл | Что меняется |
|------|-------------|
| `app/config.py` | LLM_PROVIDER default → "auto", OLLAMA_MODEL → "qwen2.5:7b" |
| `app/services/providers/registry.py` | Новая функция `get_provider_for_text(length)` |
| `app/services/summarizer.py` | Использует `get_provider_for_text` вместо `get_llm_provider` |
| `app/services/chat_engine.py` | Auto-выбор провайдера по длине контекста |
| `app/routers/native_api.py` | Эндпоинт POST /api/settings/llm-provider принимает "auto" |
| `app/routers/folders.py` | Settings page — добавить режим "auto" |
| `ZoomHubApp/.../SettingsView.swift` | Третий radio button "Авто" |
| `app/templates/settings.html` | Кнопка "Авто" в веб-интерфейсе |

## Шаги реализации

1. **Скачать qwen2.5:7b** — `ollama pull qwen2.5:7b`
2. **Обновить config.py** — default provider → "auto", model → "qwen2.5:7b"
3. **Добавить auto-routing в registry.py** — `get_provider_for_text(length)`
4. **Обновить summarizer.py** — использовать auto-routing
5. **Обновить chat_engine.py** — auto-routing для чата
6. **Обновить API (native_api.py + folders.py)** — принимать "auto" как провайдер
7. **Обновить веб-UI (settings.html)** — кнопка "Авто"
8. **Обновить нативный UI (SettingsView.swift)** — radio "Авто"
9. **Пересобрать приложение** — build.sh
10. **Тест** — проверить что короткие → Ollama, длинные → Claude

## Open Questions
- Порог 10K символов — подобрать эмпирически после тестов
- Показывать ли пользователю какой провайдер был использован (бейдж "Ollama" / "Claude" на саммари)
