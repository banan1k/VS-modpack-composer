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
- Параллельная скачка модов с ограничением запросов и файловым кешем. После синхронизации запускается фоновая предзагрузка для `MAX_VINTAGE_STORY_VERSION`; готовые ZIP-сборки не сохраняются в `data`.
- Во время фоновой предзагрузки каждый ZIP разбирается на `modinfo.json`; точные зависимости записываются прямо в SQLite сразу после разбора. Повторное сканирование Discord после prefetch не требуется: UI перечитывает каталог по завершении фоновой задачи. Обнаруженные зависимости, отсутствовавшие в Discord-каталоге, могут быть добавлены как `origin=dependency` и обрабатываются дополнительными ограниченными проходами.
- Обработка `modinfo.json` выполняется вне фонового сетевого priority-slot, чтобы пользовательский запрос не зависал на вложенном ожидании при разрешении новой зависимости. Список релизов зависимости загружается явным SQL-запросом; ORM relationship не запускает неявный async I/O.
- Кеширование обложек в `data/cache/images` с локальной раздачей через `/media/images/{mod_id}`.
- Компактное описание карточки берётся из `/api/mods`; внутренний API `modid` и публичный `assetid` `/show/mod/N` хранятся раздельно, а исходный URL из Discord сохраняется как ссылка на страницу. Это не допускает подмены Ad Astra/Clothing Visually Degrades/Immersive Quicklime из-за совпавших числовых ID.
- Shared-build URL `/build/{share_id}` восстанавливает состав сборки при открытии на другом устройстве.
- API/полоска прогресса для долгих операций; прогресс вынесен в верхнюю шапку.
- Поиск по локальному каталогу, список выбранных модов с удалением поштучно, последний поддерживаемый game-version на карточке и кликабельный Mod ID.
- Название сборки перед публикацией и Discord Embed без длинного списка модов; новые публикации прикрепляют только `modpack.json`.
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


## Version 0.8.0 / background cache and exact dependencies

This release is designed to repair catalogs created by older versions. Existing `data/app.db` may be kept. On the first sync after upgrading, the source/parser version is included in the Discord source fingerprint and triggers a one-time refresh of old records. No renaming of the `data` directory is required for `pip install -e .`: setuptools is explicitly configured to package only `app` and `app.*`.

For a successful Discord scan, the registry is replaced only when the scan completes without thread-fetch errors. A partial scan never removes previously known sources.


### Исправление фоновой предзагрузки

Если V2 `install-information` содержит элементы с `errorCode`, worker сначала проверяет уже кешированный релиз на совместимость с `MAX_VINTAGE_STORY_VERSION`. Эта проверка работает с объектом `ModRelease`; ошибка передачи декодированного списка тегов вместо объекта могла аварийно остановить предзагрузку ещё до начала скачивания. В версии 0.8.0 это исправлено.

Файловая загрузка удерживает приоритетный background-slot только во время сетевой операции. Обработка локального `modinfo.json` и разрешение его точных зависимостей выполняются отдельно, поэтому приход пользовательской задачи не может попасть в цикл ожидания из-за вложенного background-slot.

### Фоновая предзагрузка и точные зависимости

После синхронизации Discord каталог автоматически запускает фоновую предзагрузку архивов для `MAX_VINTAGE_STORY_VERSION`. По умолчанию одновременно выполняются только `PREFETCH_CONCURRENCY=2` загрузки. Через `PriorityCoordinator` новая фоновая работа не начинает новый HTTP-запрос, когда пришла пользовательская задача; текущие фоновые запросы завершаются, затем пользовательская операция получает приоритет и после неё фоновые загрузки продолжаются.

Кеш управляется по URL и `ModRelease`: если Mod DB возвращает более новый релиз, старый архив удаляется; если текущий `MAX_VINTAGE_STORY_VERSION` больше не допускает старый кешированный релиз, архив удаляется. Перед каждым скачиванием также читается локальный `modinfo.json`, что дёшево по ресурсам и позволяет восстанавливать точные зависимости после перезапуска приложения без повторного HTTP-запроса.

В `modinfo.json` верхнеуровневый объект `dependencies` интерпретируется как точная карта `modid -> минимальная версия`. Ключи `game`, `survival` и `creative` относятся к встроенным компонентам игры и не создают карточки/предупреждения о сторонних модах. Остальные идентификаторы сначала сопоставляются с обычными Discord-записями (в том числе по каноническому заголовку форума). Автоматически импортируются только зависимости, уверенно определённые как библиотеки; контентные моды не скачиваются автоматически, а создают запрос в `#mod-registry`. Официальный Mod DB API принимает строковый `modid` напрямую через `/api/mod/{modid}`. citeturn368157search1turn368157search3turn368157search11

Аддоны из сообщений Discord-тредов также становятся полноценными карточками. Выбор аддона автоматически выбирает родительский мод; родитель получает пометку `Зависимость <название>` в карточке и в списке выбранных модов. Та же логика используется для точных зависимостей из `modinfo.json`.

### Версионная политика кеша

`MAX_VINTAGE_STORY_VERSION` — глобальный потолок версии Vintage Story для каталога и фоновой предзагрузки. Например, при `MAX_VINTAGE_STORY_VERSION=1.22.6` мод с диапазоном `1.22.0 - 1.22.7` отображается как совместимый с `1.22.6`, а V2 `install-information` получает именно релиз модификации, выбранный Mod DB для игры 1.22.6. Старый кешированный релиз заменяется при обнаружении более нового релиза; при изменении потолка версии фоновой worker не оставляет явно несовместимый с новым потолком архив.


## Dependency-only libraries and manual overrides (v0.9)

- `game`, `survival`, and `creative` are core Vintage Story components bundled with the game and are not imported/downloaded as catalog mods.
- Only positive library matches are automatically imported as `origin=dependency` and prefetched. The default known library IDs are `imgui`, `libgui`, and `musicvolumebalancer`; more can be configured in `AUTO_LIBRARY_MODIDS`.
- A missing dependency that does not confidently match a library is not silently added/downloaded. The bot creates a request in `#mod-registry`, including the dependency identifier and the source mods requiring it.
- To satisfy a modinfo dependency with a fix/fork, add a Discord Forum post whose **title matches the canonical dependency name** (e.g. `Ancient Armory`) and whose first post points to the fixed/forked Mod DB page. The post title becomes `canonical_id`, distinct from the real modinfo ID read from the linked page. The app then resolves the dependency to that curated replacement.
- Cached archives use readable, per-mod paths like `data/cache/files/Realistic-Water-[realisticwater]/realisticwater_0.4.0.zip`; old numeric directories and hash-prefixed filenames are migrated on startup. Local modinfo version metadata and missing file URLs are reconciled back into the matching release row so build validation agrees with the downloaded archive.
- New Discord build posts attach only a versioned `modpack.json` manifest to avoid channel clutter. When a local Build row is missing, the app restores it from JSON; older posts that only have `modpack.txt` remain readable for backward compatibility if every named mod can be matched.


### Version 0.9.0 dependency import and cache cleanup

At startup, dependency-only records left by earlier versions are deduplicated. Built-in `game` / `survival` / `creative` rows are excluded. Legacy dependency-origin rows that are not libraries are hidden from the normal catalog and converted to unresolved dependency edges; historical build references are retained. If a Discord forum title deliberately aliases the required `modinfo` ID (for example `Ancient Armory` whose first post links to a 1.22 fix), the alias is used without mixing the original mod's release list into the fix.

Unknown non-library dependencies generate a durable `DependencyRequest` row and a bot request in `#mod-registry` when Discord is available. Repeated scans aggregate which mods need that dependency instead of posting duplicates. `AUTO_LIBRARY_MODIDS` is an explicit allowlist for libraries whose metadata is ambiguous.

Old hash-prefixed cached ZIP filenames are migrated to per-mod directories and the 16-hex prefix is stripped. The prefix is not required because each mod now has its own cache directory. Build recovery from Discord considers hidden legacy rows only while trying to reconstruct a historical manifest; those rows do not re-enter the normal catalog or dependency auto-selection.


## Version 0.10.1 / cache paths, relation deduplication and build download repair

- Cached ZIP files are stored under readable per-mod directories (`<Mod Name> [<modinfo-id>]`) rather than folders named by the local SQLite row number. Startup migration moves legacy numeric paths, strips the old redundant 16-character filename prefix, and avoids pointing a cache row at a same-named file with different content.
- Catalog identity merges and relation-target resolution plan final unique keys before applying changes. Duplicate dependency/compatibility edges are merged and deleted before key updates are flushed, preventing SQLite `UNIQUE(mod_id, target_mod_id, relation_type)` collisions during autoflush.
- A build stores the exact V2 install-information file URL/name returned for its target Vintage Story version. If an old `ModRelease` record lacks a usable URL, downloads try V2 install-information and repair release metadata before reporting an unavailable file. Validation warnings show the latest known mod version separately from the Vintage Story game version.
- New Discord build posts attach only `modpack.json`. Legacy `modpack.txt` messages remain supported as recovery input, but no text manifest is posted for new builds.


## Version 0.10.2 / build download fallback for untagged releases

- Build creation first uses V2 `install-information` for the requested Vintage Story version. If the user proceeds despite a compatibility warning and Mod DB has no release tagged for that game version, it resolves the latest non-retracted file through V2 `/mods/{internalModDbId}/releases/latest?identifier={modinfoId}`.
- Historical build resolution also reuses cached file metadata by owning mod, then tries target-version install information, the latest-release endpoint and finally the newest local downloadable release. This prevents an empty V1 `file_url` from blocking a confirmed build override.
- When several releases share a filename (e.g. `FixedPaths.zip`), the resolver matches release URLs first and then uses version-aware filename matching to avoid associating a new file with an older release.
- Added diagnostic logs for the exact unresolved mod ID, Mod DB ID, target game version and V2 error code.


## Version 0.10.3 / prefetch compatibility check fix

- Fixed a missing import for `is_version_compatible` in the prefetch service. Previously, this caused every archive entering cached-release metadata reconciliation to fail with `NameError`; now the game-version compatibility check runs as intended.
- Added a regression test covering the imported helper and compatible/incompatible release tags.
