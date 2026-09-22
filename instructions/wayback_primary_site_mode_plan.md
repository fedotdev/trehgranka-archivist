# План реализации: `WAYBACK_PRIMARY_SITE_MODE`

> Статус: план (approved, реализация не начата).
> Дата: 2026-09-21.
> Проект: `E:\trehgranka-archivist`.

## 0. Назначение и границы первой версии

Режим нужен для случаев, когда исходный сайт недоступен полностью или
частично, а рабочей точкой входа является snapshot Wayback Machine вида
`https://web.archive.org/web/<timestamp>/<original-url>`.

Основной контур первой реализации:

- базовая область: `skills/basic-media-skill`;
- реконструкция статического исторического сайта;
- Wayback как основной источник (`source_mode=wayback_primary`);
- live-ресурсы исходного домена по умолчанию не используются
  (`allow_live_fallback=false`);
- discovery по архивной HTML-странице (replay / `if_`);
- CDX-запрос для каждого original URL;
- независимый выбор capture для каждого ресурса;
- `id_` (raw replay) для сохраняемых артефактов;
- валидация каждого ответа (статус, MIME, magic bytes, декодируемость,
  пустые/заглушечные ответы, digest);
- provenance для каждого ресурса (requested vs capture timestamp,
  replay URL, status, digest, local path);
- локальная статическая копия с переписанными внутренними ссылками
  (raw-артефакты и rewritten HTML хранятся раздельно);
- SQLite / manifest без схлопывания нескольких captures одного URL;
- coverage report, различающий exact / nearest / unresolved;
- тесты и golden-сценарии (обязательный набор из 7 сценариев см. ниже).

Интеграция с `forum-media-skill` в первую версию не входит: общий pipeline
используется форумным skill, преждевременное изменение extractor-пути
увеличит риск регрессий. Форумный контур — отдельный этап после стабилизации
basic-режима.

## 1. Контракт режима

### Конфигурация

```yaml
source_mode: wayback_primary

wayback_primary:
  seed_url: "https://web.archive.org/web/20040804234004/http://metro-net.da.ru/"
  target_timestamp: "20040804234004"
  timestamp_tolerance_days: 30
  prefer_exact_timestamp: true
  use_replay_for_discovery: true
  use_id_raw_for_storage: true
  allow_live_fallback: false
  collapse_digest: true
  filter_statuscode: ["200"]
  allowed_mimetypes:
    - text/html
    - image/jpeg
    - image/png
    - image/gif
    - image/webp
    - application/pdf
    - text/css
    - application/javascript
  max_cdx_results_per_url: 50
```

Обязательные поля: `seed_url`, `target_timestamp`, `allow_live_fallback`,
`timestamp_tolerance_days`, `prefer_exact_timestamp`,
`use_replay_for_discovery`, `use_id_raw_for_storage`.
Остальные имеют значения по умолчанию.

### Статусы ресурсов

Не сводить `nearest capture`, `exact capture`, `live fallback` и
`unresolved` к одному полю `verified`. Минимальный набор статусов:

- `discovered`
- `capture_selected`
- `fetched`
- `validated`
- `stored`
- `rewritten`
- `unresolved`
- `invalid`
- `placeholder`
- `out_of_scope`
- `live_fallback` (только при явном разрешении)

### Инварианты режима

1. Wayback URL в `wayback_primary` — источник истины.
2. Live origin не вызывается без `allow_live_fallback=true`.
3. У каждого ресурса независимо выбирается capture (соседние captures
   Wayback может подставлять и единый момент не гарантирован).
4. `requested_timestamp` != `capture_timestamp` по умолчанию.
5. Replay HTML не считается raw-артефактом (для этого нужен `id_`).
6. Успех фиксируется только после валидации тела ответа.
7. Недоступные/невалидные ресурсы отражаются в coverage и provenance,
   а не молча считаются успешными.
8. Прогресс/покрытие считаются по страницам и по типам ресурсов.

## 2. Модули (архитектурные компоненты)

| Компонент | Назначение | Реализация |
|---|---|---|
| `wayback_url_parser` | Разбор replay URL, timestamp, replay mode, original URL | новый `skills/basic-media-skill/scripts/wayback_url_parser.py` |
| `cdx_client` | Запросы CDX API, нормализация captures | расширение `scripts/wayback.py` (выделить явный API) |
| `replay_fetcher` | Получение HTML для discovery (replay / `if_`) | расширение `scripts/wayback.py` |
| `raw_fetcher` | Получение raw archived responses через `id_` | расширение `scripts/wayback.py` |
| `snapshot_resolver` | Выбор capture: близость к seed, MIME/status, валидация | расширение `scripts/wayback.py` (`rank_captures`) |
| `resource_validator` | MIME, magic bytes, размеры, декодируемость, пустые ответы, заглушки | переиспользование `archivist_core.py:305-624` + archive-specific проверки |
| `link_rewriter` | Переписывание ссылок HTML/CSS на локальные пути | новый `scripts/link_rewriter.py` |
| `provenance_store` | Фиксация происхождения каждого файла | расширение SQLite/manifest (`run_pipeline.py`) |
| `coverage_reporter` | Отчёт найденные/загруженные/валидные/потерянные | расширение coverage-блока `run_pipeline.py` |

Правило: не дублировать существующую логику CDX/replay/ranking — только
выделить и расширить.

## 3. Этапы реализации

### Этап A. Контракт конфигурации и статусы

- Определить enum-семантику `source_mode` и `WAYBACK_PRIMARY_SITE_MODE`.
- Валидация новых полей конфига.
- Поведение: режим переключается автоматически при seed URL
  `web.archive.org/web/...`.

Файлы:

- `skills/basic-media-skill/scripts/archivist_core.py:767-815`
- `skills/basic-media-skill/assets/project-config.example.yaml`
- `skills/basic-media-skill/SKILL.md`

### Этап B. `wayback_url_parser.py`

Новый модуль, парсит:

- обычный replay URL (mode `page`);
- `id_`, `if_`, `im_` и составные модификаторы;
- timestamp из seed;
- original URL (http/https, query, fragment);
- original URL без timestamp (timestamp берётся из конфига/API).

Результат:

```json
{
  "source_mode": "wayback_primary",
  "seed_wayback_url": "https://web.archive.org/web/20040804234004/http://metro-net.da.ru/",
  "seed_timestamp": "20040804234004",
  "seed_replay_mode": "page",
  "seed_original_url": "http://metro-net.da.ru/",
  "seed_original_origin": "http://metro-net.da.ru"
}
```

Добавить self-check / unit-тесты на каждый тип URL.

### Этап C. CDX-клиент и независимый выбор capture

Вынести из `wayback.py` явный API:

- запрос по original URL: `url`, `output=json`,
  `fl=timestamp,original,statuscode,mimetype,digest,length`;
- фильтры `statuscode:200`, MIME;
- `collapse=digest`;
- временное окно вокруг seed timestamp;
- лимиты и пагинация (сейчас потолок `MAX_CDX_PAGES=10`,
  `wayback.py:61` — вынести в конфиг или фиксировать в отчёте);
- лимит кандидатов (сейчас `max_candidates=8`,
  `wayback.py:279-310` — из конфига).

Алгоритм выбора для каждого ресурса:

1. отфильтровать по статусу и MIME;
2. отбросить captures вне допустимого окна;
3. ранжировать по абсолютному расстоянию до `requested_timestamp`
   (не по новизне);
4. при `prefer_exact_timestamp=true` приоритет у exact capture;
5. проверять кандидатов через replay до первого прошедшего валидацию;
6. фиксировать `selected_reason`.

`selected_reason`: `exact_timestamp`, `nearest_timestamp`,
`nearest_validated`, `fallback_from_invalid_candidate`,
`source_page_recovery`, `unresolved`.

### Этап D. Archive-specific валидация

Переиспользовать существующие проверки
(`archivist_core.py:305-624`) + добавить для архивного режима:

- HTTP-статус, фактический MIME, magic bytes, длина тела, SHA-256;
- сравнение локального SHA-256 с CDX `digest`;
- пустой ответ, placeholder/заглушка;
- несоответствие расширения и MIME;
- изображения: декодируемость, ширину/высоту, HTML вместо картинки;
- HTML: наличие полезного тела, отсутствие служебной страницы Wayback,
  charset, наличие ссылок для обхода.

Формат результата:

```json
{
  "validation_status": "verified",
  "validation_errors": [],
  "magic_mime_match": true,
  "sha256": "...",
  "cdx_digest_match": false,
  "width": 640,
  "height": 480
}
```

### Этап E. Database / manifest state

Расширить SQLite (создаётся в `run_pipeline.py:97-107`) отдельными
сущностями:

- `seeds`
- `pages`
- `resources`
- `captures`
- `fetch_attempts`
- `validations`
- `rewrites`
- `unresolved`

Обязательные поля `captures`:

- `original_url`
- `requested_timestamp`
- `capture_timestamp`
- `replay_mode`
- `wayback_url`
- `statuscode`
- `mimetype`
- `digest`
- `length`
- `selected_reason`
- `candidate_rank`
- `validation_status`

Исправить схлопывание записей по одному URL в `_write_manifest()`
(`run_pipeline.py:502-521`): resource record отдельно от capture records.

### Этап F. Wayback-first orchestration

Изменить порядок источников в pipeline
(`run_pipeline.py:162-176`, `run_pipeline.py:204-282`):

```text
parse seed
→ build Wayback manifest
→ fetch archived HTML (replay / if_)
→ extract original resource URLs
→ query CDX independently
→ select captures
→ fetch storage responses via id_
→ validate
→ store
→ rewrite links
→ emit provenance
→ coverage report
```

При `allow_live_fallback=false`:

- live `core.fetch()` для исходного домена не вызывается;
- ошибки live не маскируются под archive result;
- в отчёте `live_fallbacks=0`.

При `allow_live_fallback=true`: отдельный source type `live_fallback` в
provenance, не записывается как `wayback_primary`.

### Этап G. Расширение discovery

Расширить `LinkExtractor` (`run_pipeline.py:39-67`) на:

- `meta refresh`, Open Graph, JSON-LD;
- CSS `url(...)`;
- `srcset`, `data-src`, `data-fallback`, `data-original`;
- ссылки, уже переписанные Wayback (восстановление original URL);
- ссылки с replay-модификаторами;
- документы и вложения.

Для каждого кандидата хранить: `discovered_from`, `discovery_type`,
`raw_value`, `resolved_original_url`, `resolved_wayback_url`,
`resource_kind`, `in_scope`.

### Этап H. Storage layout и link rewriting

Layout (только для `wayback_primary`; обычный layout для golden-сценариев
не трогать):

```text
output/
├── site/                 # rewritten локальная копия
│   ├── index.html
│   ├── pages/ media/ css/ js/ documents/
├── raw/                  # raw archived артефакты (id_)
│   ├── html/ resources/
├── manifest.json
├── provenance.jsonl
├── captures.jsonl
├── rewrites.jsonl
├── unresolved.jsonl
├── database.sqlite
└── reports/ coverage.json summary.json validation.json
```

`link_rewriter.py`:

- mapping `original_url ↔ wayback_replay_url ↔ local_path`;
- переписывание HTML и CSS;
- unresolved-URL не заменять на несуществующие файлы;
- внешние/out-of-scope ссылки остаются внешними;
- фрагменты и query учитывать; коллизии имён — детерминированно;
- каждое переписывание фиксировать в `rewrites`.

### Этап I. Provenance

Для каждого ресурса:

```json
{
  "entity_type": "image",
  "original_url": "http://metro-net.da.ru/img/map.gif",
  "seed_timestamp": "20040804234004",
  "selected_capture_timestamp": "20040801124510",
  "replay_mode": "id_",
  "wayback_replay_url": "https://web.archive.org/web/20040801124510id_/http://metro-net.da.ru/img/map.gif",
  "statuscode": "200",
  "mimetype": "image/gif",
  "digest": "....",
  "length": 18234,
  "local_path": "media/img/map.gif",
  "validation_status": "verified"
}
```

Различать:

- requested timestamp;
- selected capture timestamp;
- actual downloaded replay URL;
- local output path.

### Этап J. Coverage report

Блок в отчёте:

```json
{
  "wayback_primary": {
    "enabled": true,
    "seed_timestamp": "20040804234004",
    "discovered_html": 10,
    "fetched_html": 9,
    "valid_html": 8,
    "discovered_media": 42,
    "fetched_media": 38,
    "valid_media": 35,
    "exact_timestamp": 12,
    "nearest_timestamp": 23,
    "unresolved": 4,
    "placeholder_or_corrupt": 3,
    "skipped_out_of_scope": 2,
    "live_fallbacks": 0,
    "rewritten_links": 97
  }
}
```

Устранить рассогласование между recovery queue и фактически
recovered ресурсами (`run_pipeline.py:426-435`, `run_pipeline.py:469-484`,
`validate_report.py:55-58`).

### Этап K. CLI

Расширить `run_pipeline.py:626-668`:

```bash
python run_pipeline.py \
  --config project.yaml \
  --source-mode wayback_primary \
  --wayback-seed-url URL \
  --wayback-target-timestamp TIMESTAMP \
  --allow-live-fallback \
  --timestamp-tolerance-days N \
  --prefer-exact-timestamp \
  --store-raw-html \
  --emit-provenance-jsonl \
  --output report.json
```

Приоритет: CLI flag → config file → derived from seed URL → default.
Автоопределение `wayback_primary` по seed URL.
При конфликте CLI timestamp и seed timestamp: предпочесть CLI и
зафиксировать `timestamp_source`, либо ошибка с понятным сообщением.

### Этап L. Валидатор отчёта, документация, evals

- `validate_report.py`: новые инварианты (exact/nearest/unresolved,
  queue vs recovered, отсутствие live fetch при запрете);
- `project-config.example.yaml`: новый раздел конфига;
- `SKILL.md`: блок WAYBACK_PRIMARY_SITE_MODE (Do / Do not);
- `evals/basic-media-skill.eval.md`: golden-критерии для primary mode.

## 4. Режимы деградации (должны обрабатываться явно, статусом)

1. HTML есть, часть media отсутствует.
2. Прямой capture media отсутствует, но есть archived page — восстановить
   исторический URL из неё (текущий fallback: `wayback.py:350-416`).
3. CSS/JS утеряны, но HTML доступен.
4. Ресурсы подставлены из другого timestamp (зафиксировать расхождение).
5. Replay даёт один результат, `id_` — другой (два validation records,
   явный статус расхождения).
6. Файл в CDX есть, но replay отдаёт невалидный контент.

Ни в одном из случаев ресурс не отмечается успешным молча.

## 5. Тестовая стратегия

В репозитории нет обычного `tests/` — добавить изолированный test suite
(first version) + сохранить существующие self-check и evals.

### Unit-тесты

- parser: page / `id_` / `if_` / `im_` / query / trailing slash;
- ranking: exact vs nearest vs newest (newest не выигрывает
  автоматически), invalid nearest → следующий валидный;
- восстановление original URL из Wayback-ссылки, relative/absolute,
  srcset, CSS URL;
- валидация: HTML вместо image, пустой ответ, placeholder, корректный
  GIF/JPEG/PNG, расхождение replay vs `id_`;
- link rewriting: local success, unresolved, external, fragment/query,
  collisions.

### Обязательные integration/golden-сценарии

1. Сайт существует только в Wayback.
2. HTML имеет exact capture, картинка — только nearest capture.
3. Direct image capture отсутствует, исторический URL восстановлен из
   archived page.
4. Replay URL рабочий, `id_` отдаёт невалидный ответ.
5. Часть ресурсов ведёт на другой timestamp.
6. Относительные ссылки корректно переписываются локально.
7. При `allow_live_fallback=false` live fetch не происходит.

Для стабильности тестов: локальный mock HTTP/CDX/replay server; реальный
Wayback — только smoke/eval-сценарий, не единственный источник проверки.

## 6. Критерии готовности первой версии

1. Seed Wayback URL корректно парсится (все modes).
2. Discovery выполняется по архивной версии, без обращения к live origin.
3. CDX запрашивается для каждого original URL.
4. Capture выбирается по близости к target timestamp и валидации,
   а не по новизне.
5. Сохраняемые артефакты загружаются через `id_`.
6. Каждый ответ проходит независимую валидацию.
7. Raw и rewritten HTML хранятся раздельно.
8. Локальная копия открывается без зависимости от `web.archive.org`.
9. Provenance фиксируется для каждого файла.
10. Coverage различает exact / nearest / unresolved.
11. Manifest/SQLite не теряют несколько captures одного URL.
12. Семь обязательных тестовых сценариев проходят.
13. `validate_report.py` принимает новый формат отчёта.
14. `--allow-live-fallback` корректно запрещает/разрешает live.

## 7. Блок для AGENTS/spec

```text
WAYBACK_PRIMARY_SITE_MODE

If the input URL is a Wayback Machine replay URL or the target site exists only in web.archive.org, treat Internet Archive as the primary source of truth.

Do:
- parse seed timestamp, replay mode and original URL;
- use archived HTML for discovery;
- query CDX for each original URL;
- choose captures by temporal proximity and validation result;
- use replay/if_ for navigation and id_ for storage;
- validate every HTML/media response independently;
- record provenance for every resource;
- rebuild a local static copy with rewritten internal links.

Do not:
- silently fetch live resources from the original domain;
- assume all embedded resources share the seed timestamp;
- treat replay HTML as raw archived content when id_ is required;
- mark a resource successful before validation;
- claim full restoration without unresolved-resource accounting.
```

## 8. Порядок работ (этапы-коммиты)

1. Этап A+B: контракт конфига, статусы, `wayback_url_parser.py`
   (вертикальный срез, минимальный).
2. Этап C+D: CDX/capture API, archive-валидация.
3. Этап E: SQLite/manifest model.
4. Этап F: Wayback-first orchestration.
5. Этап G+H: расширение discovery, storage через `id_`,
   `link_rewriter.py`.
6. Этап I+J: provenance, coverage.
7. Этап K: CLI.
8. Этап L + тесты: suite, golden, документация, evals.
9. Отдельно (после стабилизации): интеграция с `forum-media-skill`.

Первый коммит — минимальный работающий вертикальный срез:
seed parse → CDX → capture select → recovery validate → store → provenance.