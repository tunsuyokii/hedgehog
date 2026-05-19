# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a single-process Python Telegram bot ("hedgehog") that orchestrates Telegram → Cursor Cloud → GitHub workflows. There is no database, no Docker, and no frontend to run locally. The bot is started with `python src/bot.py` from the repo root.

### Running the bot

The bot requires two environment variables to start: `TELEGRAM_TOKEN` and `PROXYAPI_TOKEN`. Set them in a `.env` file (see `.env.example`) or export them. Without valid tokens, the OpenAI client raises at module-import time and the Telegram polling loop rejects the token.

Additional optional services degrade gracefully when credentials are missing:
- **Cursor Cloud** (`CURSOR_API_KEY`) — `CursorRunner.enabled` returns `False`
- **GitHub** (`GITHUB_TOKEN`) — `GitHubRepoClient.enabled` returns `False`
- **S3 Audit** (`S3_BUCKET`, `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`) — `S3AuditStore.enabled` returns `False`; all write calls become no-ops

### Lint / Compile checks

There is no linter configured. CI runs compile checks only:

```bash
python -m py_compile src/bot.py
python -m py_compile src/models.py
python -m py_compile src/security_guard.py
python -m py_compile src/spec_flow.py
python -m py_compile src/cursor_runner.py
python -m py_compile src/s3_store.py
python -m py_compile src/git_policy.py
python -m py_compile src/identity_policy.py
```

### Tests

There is no test suite. Validate changes with the compile checks above and by verifying imports:

```bash
cd src && python -c "from bot import _split_text_for_telegram; print('OK')"
```

Note: importing `bot` at module level requires `PROXYAPI_TOKEN` to be set (even a dummy value works for import checks) because the `OpenAI` client is instantiated at module scope.

### Gotchas

- The `src/` directory is not a package (no `__init__.py`). All modules use relative imports within `src/`, so run scripts with `cwd` set to `src/` or the repo root with `python src/bot.py`.
- `PROXYAPI_TOKEN` must be set even for import-level checks of `bot.py` because the `OpenAI(api_key=...)` call happens at module scope.
- Dependencies are installed via `pip install -r requirements.txt` (no virtualenv enforced).
