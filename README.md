# Vintage Story Modpack Builder

Локальное веб-приложение + Discord-бот для формирования ZIP-сборок Vintage Story на базе каталога из Discord и данных Vintage Story Mod DB.

## Что уже реализовано

- Discord ingestion: явный список Forum Channel ID → посты модов → starter message + ветки addon-сообщений.
- Локальное SQLite-хранилище: `Mod`, `ModRelease`, `Addon`, `Dependency`, `Compatibility`, `DiscordSource`, `Build`, `BuildMod`, `CachedFile`, `Job`.
- Инкрементальная синхронизация: при неизменившемся Discord source hash Mod DB запись не перезапрашивается; после обновления алгоритмов `catalog_source_version` один раз принудительно обновляет старые записи; новая версия `8` также выполняет repair pass для старых неверно сопоставленных Mod DB identity.
- Mod DB V1 API для основной карточки и релизов; `/show/mod/N` никогда не используется напрямую как `/api/mod/N`: asset ID и внутренний API mod ID разрешаются раздельно. V2 `install-information` оставлен в клиенте для последующего точного resolver-пути под конкретную версию игры.
- Многоступенчатый анализ зависимостей/совместимости только по блоку описания Mod DB: сначала API-данные, затем HTML + локальная база, с типом связи, confidence и `verified=false`; прямые ссылки имеют приоритет, несколько ссылок в одном `Requires` сохраняются отдельно, блок скачивания/релизы и Comments исключаются, а свободные ссылки без явного контекста получают confidence ниже порога 90%; комментарии, release table, changelog и блок `Recommended download` не анализируются.
- Проверка сборки перед созданием: отсутствующие обязательные зависимости, incompatibility и отсутствие релиза для целевой версии VS.
- Share ID вместо большого JSON в URL.
- Discord-публикация готовой сборки.
- Параллельная скачка модов с ограничением запросов и файловым кешем. Скомпилированные ZIP-сборки не сохраняются в `data`: при скачивании архив временно создаётся в системном temp и после ответа удаляется.
- Кеширование обложек в `data/cache/images` с локальной раздачей через `/media/images/{mod_id}`.
- Компактное описание карточки берётся из `/api/mods`; внутренний API `modid` и публичный `assetid` `/show/mod/N` хранятся раздельно, а исходный URL из Discord сохраняется как ссылка на страницу. Это не допускает подмены Ad Astra/Clothing Visually Degrades/Immersive Quicklime из-за совпавших числовых ID.
- Shared-build URL `/build/{share_id}` восстанавливает состав сборки при открытии на другом устройстве.
- API/полоска прогресса для долгих операций; прогресс вынесен в верхнюю шапку.
- Поиск по локальному каталогу, список выбранных модов с удалением поштучно, последний поддерживаемый game-version на карточке и кликабельный Mod ID.
- Название сборки перед публикацией и Discord Embed без длинного списка модов; полный состав прикрепляется единым `modpack.txt`.
- `#mod-registry` получает одно сообщение с `mod-registry.txt` как attachment вместо разрезанного JSON; старые registry-сообщения бот удаляет при следующей синхронизации.

## Запуск

1. Установите Python 3.12+.
2. Создайте `.env` на основе `.env.example`.
3. Запустите:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Откройте `http://127.0.0.1:8000`.

## Discord

Создайте Discord application + bot. Боту нужны права на просмотр указанных каналов, чтение истории сообщений и отправку сообщений в registry/builds канал.

В `.env` задайте:

```env
DISCORD_TOKEN=...
DISCORD_GUILD_ID=123456789012345678
DISCORD_FORUM_CHANNEL_IDS=111111111111111111,222222222222222222,333333333333333333
```

`DISCORD_FORUM_CHANNEL_IDS` — это именно ID форумных каналов с модами. Категория Discord `Mods` больше не используется для поиска каналов: это делает настройку стабильнее и исключает ошибки из-за названия категории. Можно указать любое количество каналов через запятую или `;`.

Для публикации сборок задайте `DISCORD_BUILDS_CHANNEL_ID`. Для машинного registry задайте `DISCORD_REGISTRY_CHANNEL_ID`.

### Важно: Message Content Intent

Бот читает текст стартового сообщения темы и сообщений внутри веток, поэтому в Discord Developer Portal нужно включить:

`Applications → ваш Bot → Privileged Gateway Intents → Message Content Intent`

После изменения настройки перезапустите приложение. Без этого Discord подключается с ошибкой `PrivilegedIntentsRequired`, а синхронизация каталога не сможет прочитать ссылки на Mod DB.

`PUBLIC_BASE_URL` должен быть URL, по которому пользователи Discord действительно смогут открыть локальный сайт. Для чисто локального использования `http://localhost:8000` подходит только для текущей машины.

## Замечания по Mod DB

Официальная документация репозитория Mod DB описывает `/api/mod/{modid}` и формат JSON с релизами; API также указывает, что возвращаемые URI нужно использовать как есть. V2 `install-information` может вернуть `fileName`/`fileUrl` для конкретной версии игры. Именно поэтому загрузчик не строит URL ZIP вручную, когда API уже дал полный URI.

## Правила связей

Автоматические связи с confidence ниже `RELATION_MIN_CONFIDENCE` (по умолчанию 90%) остаются видимыми для ручной проверки, но не влияют на проверку/создание сборки. Прямой Mod DB link рядом с `Requires`/`Depends on`/`Compatible`/`Conflicts` имеет приоритет. Несколько ссылок в одном блоке сохраняются как отдельные отношения. Ссылка без явного контекста трактуется как потенциальная опциональная зависимость с confidence 89%. Текст после `Recommended download...`, таблица релизов и комментарии не участвуют в relationship pass.

## Дальнейшие улучшения

- Добавить чтение `mod-registry.txt` как резервного индекса при восстановлении каталога.
- Сохранять HTML source hash отдельно и повторно выполнять dependency pass только при изменении страницы Mod DB.
- Добавить ручную модерацию/подтверждение автоматически найденных связей.
- Вынести worker в отдельный процесс/очередь, если объём каталога станет существенно больше.

> Note: SQLAlchemy asyncio support requires the `greenlet` package. The project now declares `SQLAlchemy[asyncio]` so a clean install pulls it automatically.


### Синхронизация и скорость

Сканирование Discord выполняется параллельно с ограничением `HTTP_CONCURRENCY`, а прогресс показывает `текущий/всего` форумных постов, найденные моды, пропуски и ошибки. Mod DB-записи сохраняются по одной по мере завершения, поэтому каталог не ждёт завершения всех HTTP-запросов. Запрос страницы Mod DB и API для одного изменившегося мода выполняются параллельно; сами моды также обрабатываются параллельно. SQLite работает в WAL-режиме с `busy_timeout`, чтобы polling задач и короткие записи каталога не блокировали друг друга.

Карточка предпочитает метаданные точной страницы Mod DB (`og:title`, `og:description`, `og:image`), затем использует `/api/mods`. Это исключает подмену названия/превью другой записью API; подробное описание страницы сохраняется отдельно. Подробное описание страницы сохраняется отдельно в поле `description`.

## Windows launcher and persistent logs

For Windows you can start the server with `start.bat`. It uses the project's `.venv`, starts Uvicorn on `0.0.0.0:8000`, keeps the console visible, and appends the console stream to `data/logs/server-console.log`.

The application itself also writes structured logs to `data/logs/app.log` with rotation (5 backups, 10 MB each). These logs persist between sessions and include Discord scan counts, every saved Mod DB identity, partial/fallback states and relation-parser failures.

The package configuration explicitly discovers only `app` / `app.*`, so the project's `data` directory is never interpreted as a Python package during `pip install -e .`.


## Version 0.5.1 / catalog repair

This release is designed to repair catalogs created by older versions. Existing `data/app.db` may be kept. On the first sync after upgrading, the source/parser version is included in the Discord source fingerprint and triggers a one-time refresh of old records. No renaming of the `data` directory is required for `pip install -e .`: setuptools is explicitly configured to package only `app` and `app.*`.

For a successful Discord scan, the registry is replaced only when the scan completes without thread-fetch errors. A partial scan never removes previously known sources.
