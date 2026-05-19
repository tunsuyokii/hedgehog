# Telegram Multi-Agent Assistant via ProxyAPI

Телеграм-бот, который:
- получает идею пользователя;
- запускает блок "Душнила" (уточнения и финализация задачи);
- прогоняет спор агентов в несколько циклов;
- отдает итог от "Менеджера";
- работает с файлами (документы из Telegram загружаются в Assistant API);
- ведет полный протокол в `logs/chat_<chat_id>.txt`.

## Архитектура пайплайна

1. **Душнила**
   - генерирует до 5 уточняющих вопросов;
   - после ответа пользователя формулирует итоговую постановку.

2. **Discovery + Решалы** (в циклах `DISCOVERY_CYCLES`)
   - системный аналитик;
   - поклонник AI;
   - фанат качества;
   - критик;
   - позитивчик.

3. **Менеджер**
   - принимает финальное решение по реализации.

## Требования

- Python 3.10+
- Telegram bot token
- ProxyAPI token

## Quickstart (Windows / PowerShell)

1. Создай и активируй виртуальное окружение:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

2. Установи зависимости:

   ```powershell
   pip install -r requirements.txt
   ```

3. Создай файл `.env` и заполни переменные окружения из разделов ниже.

4. Запусти бота:

   ```powershell
   python src/bot.py
   ```

### Обязательные переменные окружения

- `TELEGRAM_TOKEN`
- `PROXYAPI_TOKEN`
- `CURSOR_API_KEY`
- `GITHUB_TOKEN`
- `GITHUB_REPO` — например `tunsuyokii/hedgehog`

### Обязательные переменные для S3-аудита

- `S3_ENDPOINT`
- `S3_BUCKET`
- `S3_ACCESS_KEY`
- `S3_SECRET_KEY`

### Рекомендуемые переменные окружения

Можно не задавать, если подходят значения по умолчанию:

- `PROXY_BASE_URL=https://api.proxyapi.ru/openai/v1`
- `CASUAL_MODEL_NAME=gpt-5.5`
- `SECURITY_MODEL_NAME=gpt-5.5`
- `SPEC_MODEL_NAME=gpt-5.5`
- `MAX_REPLY_CHARS=3500`
- `ALLOWED_USERNAMES=tunsuyoki`
- `CURSOR_API_BASE=https://api.cursor.com/v1` — если используется другой endpoint, поменяй значение
- `S3_PREFIX=telegram-cursor`

## Команды бота

- `/start` — приветствие и краткая инструкция
- `/reset` — сброс состояния чата и удаление лога `logs/chat_<chat_id>.txt`

## Как работает с файлами

1. Отправь файл боту (document).
2. Бот скачает его в `uploads/`.
3. Бот загрузит файл в Assistant API (`purpose=assistants`).
4. Следующие агенты получают файл через attachments и могут использовать его в анализе.

## Важные примечания

- Этот проект использует OpenAI-совместимый endpoint ProxyAPI:
  - `https://api.proxyapi.ru/openai/v1`
- Токены не храните в коде; только в `.env`.
- Если токены уже были опубликованы, обязательно перевыпустите их.
