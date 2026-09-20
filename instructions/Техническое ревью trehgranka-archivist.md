# Техническое ревью `trehgranka-archivist`

## Вердикт

Репозиторий сейчас следует считать **хорошо оформленным прототипом спецификации и офлайн-демонстратором**, но не готовым архиватором. Публичное описание обещает цепочку `download → hash → validate → Wayback-recover → coverage-report`, тогда как исполняемый pipeline фактически читает подготовленный JSONL-манифест, выполняет поверхностные проверки, ставит сбои в очередь и выпускает отчёты; массовое скачивание, реальное восстановление Wayback, проверка байтов и полноценный full run отсутствуют.

Проверены все 74 отслеживаемых файла на ветке `main`: два skill-пакета, Python- и shell-код, manifests, fixtures, инструкции и примеры конфигурации. Репозиторий пока состоит из одного коммита, поэтому стабильность поведения и миграции схем историей изменений не подтверждаются.

**Рекомендация:** до устранения P0-дефектов переименовать статус в `experimental/prototype`, не запускать на больших сайтах и не считать результаты доказуемым архивом.

## Что проверено

- `python -m compileall`: весь Python-код компилируется.
- `bash -n`: оба Unix-инсталлятора синтаксически корректны.
- Plugin/marketplace manifests: корректный JSON.
- Оба `install.sh`: успешно установились в изолированные временные `$HOME`.
- Оба eval-spec: проходят `--validate`.
- Rollout: 15/15 проверок basic и 18/18 forum формально прошли.
- Выполнены поведенческие пробы full-run gate, повторного запуска, хеширования, robots, URL-нормализации и Wayback-ranking.
- Проверены Markdown-ссылки, кодировки, заявления о лицензии, признаки секретов и случайно добавленные runtime-файлы.
- PowerShell-инсталлятор просмотрен статически; фактический запуск невозможен из-за отсутствия `pwsh` в среде ревью.
- GitHub secret scanning запустить не удалось: для репозитория не включён GitHub Advanced Security. Локальный поиск типовых credential-паттернов явных секретов не выявил.

## Критические дефекты

| Приоритет | Дефект | Последствие | Немедленное действие |
|---|---|---|---|
| P0 | Нет заявленной нормативной спецификации `instructions/universal_web_forum_media_archivist_prompt.md` | README, `AGENTS.md` и frontmatter skills ссылаются на отсутствующий источник истины; невозможно проверить соответствие реализации | Добавить `instructions/` и CI-проверку всех внутренних путей либо удалить заявление о canonical spec |
| P0 | «Full run» ничего не запускает | При двух флагах gate сообщает `proceed`, но downloader/crawler не вызывается, файлы не скачиваются; создаётся ложное впечатление завершённого архива | До реализации возвращать `not_implemented` и ненулевой exit code; затем добавить отдельный state machine full run |
| P0 | SHA-256 вычисляется не по файлу | `_sha256()` игнорирует `data` и хеширует `body` либо URL; provenance и дедупликация недостоверны | Хешировать точные загруженные bytes потоково; хранить byte length и алгоритм |
| P0 | Валидация принимает отсутствующий контент за валидный | Media с `status=200`, MIME и без bytes считается valid; `text/html` разрешён для media; basic-версия может пропустить строковый `"404"` | Сделать bytes обязательными для `verified`; отделить URL-discovered от downloaded/decoded/verified |
| P0 | Wayback recovery не работает как заявлено | Клиент запрашивает `output=json`, но разбирает ответ построчно как CDX text; helper только ранжирует метаданные и не скачивает replay `id_`, не валидирует и не сохраняет файл | Исправить формат ответа, реализовать replay/download/validation/provenance и интегрировать worker с pipeline |
| P0 | `robots.txt` проверяется и трактуется неверно | Для target с путём запрашивается `.../path/robots.txt`, правила не вычисляются для конкретного URL, ошибки не блокируют run; full gate не зависит от robots | Использовать origin-root и `urllib.robotparser`, фиксировать policy per URL/user-agent, fail closed при обязательном режиме |
| P0 | Gate проверяет только два булевых флага | `proceed` возможен при unresolved recovery, непроверенной пагинации, `originals_vs_thumbnails=false`, robots error и verdict `needs changes` | Gate должен зависеть от readiness-invariants, а не только подтверждения пользователя |

### Отсутствующая спецификация

[README](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/README.md#L3-L15) и [AGENTS.md](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/AGENTS.md#L3-L9) называют `instructions/...` нормативным источником, но корень репозитория содержит только `.gitignore`, `AGENTS.md`, `README.md` и `skills/`. Более того, относительная ссылка `../instructions/...` в skill frontmatter из `skills/basic-media-skill` указывает на `skills/instructions`, а не на корневой `instructions`.

Исправление должно включать:

- добавление canonical-файла;
- ссылки вида `../../instructions/...` из каждого skill;
- CI-тест существования всех `source_references`;
- явную версию спецификации и совместимую версию pipeline/report schema.

### Ложный full run

В [`run_pipeline.py`](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/skills/basic-media-skill/scripts/run_pipeline.py#L129-L157) всегда выполняются preflight, discovery, dry-run, validation, создание recovery queue и отчётов. [`_stage_full_run_gate()`](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/skills/basic-media-skill/scripts/run_pipeline.py#L398-L411) лишь меняет `stop` на `proceed`; после этого crawler или downloader не запускается.

Поведенческая проба с `USER_CONFIRMED_FULL_RUN=true` и `--run-full` вернула `action=proceed`, создала `final_report.json`, но каталог media остался пустым. Это опасный контракт: оператор может принять метаданные подготовленного fixture за выполненное архивирование.

До реализации full run безопасный контракт должен быть таким:

```json
{
  "gate": "blocked",
  "reason": "full_run_not_implemented",
  "exit_code": 3
}
```

### Недостоверные хеши

Обе реализации используют эквивалент:

```python
payload = str(item.get("body") or item.get("url") or "").encode("utf-8")
return hashlib.sha256(payload).hexdigest()
```

Поле `data` с фактическими bytes при этом не участвует. Тестовый JPEG `ffd8ff00` имеет один SHA-256, но pipeline записывает SHA-256 строки URL. Это нарушает центральное обещание «download → hash → validate» и делает невозможными доказуемую целостность, дедупликацию и сравнение live/Wayback.

Правильная модель:

- `sha256_payload`: потоковый хеш точных response bytes;
- `sha256_normalized`: только как отдельное необязательное поле для преобразованного объекта;
- `http_body_length`, `stored_file_length`, `content_encoding`, `transfer_decoded`;
- временный файл, `fsync`, затем атомарный rename;
- повторное чтение сохранённого файла и контроль хеша.

### Слабая валидация

[`_validate_media_item()`](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/skills/basic-media-skill/scripts/run_pipeline.py#L308-L327) проверяет только несколько HTTP-кодов, пустую строку `body`, узкий набор MIME и первые magic bytes, если `data` уже передано. Если `data` и `body` отсутствуют, item обычно признаётся валидным.

Необходимы отдельные состояния:

```text
discovered → fetched → stored → magic_verified → decoded → role_verified
          ↘ failed/retryable → recovery_queued → recovered/failed
```

Минимальные проверки: numeric final status, непустые bytes, фактический MIME по сигнатуре, полный decode изображения/PDF/ZIP, dimensions/page count, truncated-file detection, placeholder hash/pHash, maximum size, decompression-bomb limit, redirect provenance и соответствие media role.

### Неработающий Wayback

[`wayback.py`](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/skills/basic-media-skill/scripts/wayback.py#L51-L74) разбирает space-separated CDX, но запрос задаёт [`output=json`](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/skills/basic-media-skill/scripts/wayback.py#L144-L159). Даже при найденном capture код не строит replay URL, не скачивает archived response и не проверяет его bytes.

Дополнительные проблемы:

- `rank_captures()` без дат сортирует по timestamp по возрастанию, хотя текст результата заявляет лишь, что newest не выбран автоматически; политика фактически не определена.
- URL variants неправильно обрабатывают credentials: из `user:pass@example.org` строится `www.user:pass@example.org`.
- Нет дедупликации captures по digest.
- Нет фильтра MIME изображения/документа.
- Нет обработки redirect/revisit records и отсутствующего payload.
- Нет recovery через archived source page и исторические `src/srcset/data-*`.
- Forum skill не содержит helper и pipeline его не импортирует.

Нужен интеграционный тест с локальным fake-CDX и fake-replay HTTP server: 404 live → два captures, первый placeholder, второй валидный JPEG → файл сохранён, bytes-хеш подтверждён, provenance заполнен, очередь закрыта.

### Robots и ограничения

[`_fetch_robots()`](https://github.com/fedotdev/trehgranka-archivist/blob/abc7a2ebb5e4379bdb71bdf845ad991c3a54d2e6/skills/basic-media-skill/scripts/run_pipeline.py#L173-L187) строит URL через `urljoin(TARGET_URL, "robots.txt")`. Для `https://example.org/private/a` получается `https://example.org/private/robots.txt`, а должен запрашиваться `https://example.org/robots.txt`.

Поле `allows_wildcard` ищет `Allow:` с символом `*`, не применяет группы User-agent, Disallow, Crawl-delay и longest-match к целевому пути. Тесты с `Allow: /`, `Disallow: /` и смешанными правилами дали одинаковый результат. При сетевой ошибке код пишет `run continues`; full gate этого не учитывает.

Исправление:

- origin вычислять как `scheme://host/`;
- использовать `urllib.robotparser.RobotFileParser` либо проверенный эквивалент;
- проверять каждый URL перед fetch;
- при `RESPECT_ROBOTS_TXT=true` блокировать readiness, если политика запрещает URL;
- различать `robots_missing`, `robots_unavailable`, `allowed`, `disallowed`;
- owner override хранить как отдельное подписанное решение, а не как `RESPECT_ROBOTS_TXT=false`.

## Высокий приоритет

### Discovery не обнаруживает ссылки

`_small_probe()` создаёт очередь только с target URL и никогда не извлекает ссылки из HTML. Поэтому `MAX_DEPTH` фактически не используется, а discovery без подготовленного manifest исследует одну страницу. Для forum pipeline это означает отсутствие обхода категорий, тем, pagination и attachments.

Нужно реализовать bounded crawler:

- HTML parser и canonical URL normalization;
- очередь BFS с `(url, depth, referrer, relation)`;
- allowed-origin и redirect checks после каждого redirect;
- dedup по canonical URL;
- извлечение `href`, `src`, `srcset`, lazy-load attributes, OG/JSON-LD;
- rate delay, retries, exponential backoff и обработку 429/503;
- persistence/resume в SQLite.

### IPS extractor не подключён

В forum pipeline `extractors/invision.py` используется только для `detect(html)`. Функция `extract()` нигде из orchestrator не вызывается; entity counts берутся из уже подготовленного JSONL. Поэтому заявление о встроенном deterministic IPS extractor верно только как наличие отдельного файла, но не как end-to-end функция.

Нужно добавить adapter contract:

```python
extract_page(html_bytes, final_url) -> {
  entities, discovered_urls, pagination, attachments, warnings
}
```

Затем подключить его в discovery/dry run, сохранять raw HTML fixture и сверять counts с контрольным extractor на нескольких шаблонах IPS.

### Coverage математически несопоставим

Сейчас denominator — все discovered URLs, включая страницы, а numerator — только valid media. Поэтому fixture с двумя страницами и пятью media даёт `4/7 = 0.57`, хотя это не coverage ни страниц, ни media. Для форума нужны независимые показатели:

- page fetch coverage;
- media byte-verification coverage;
- forum/topic/post coverage;
- pagination closure;
- attachment coverage;
- recovery closure;
- unresolved/error budget.

Общий статус нельзя сворачивать в одну долю без явных весов.

### Gate не проверяет readiness

Состояние `proceed` должно требовать одновременно:

- `test_report.verdict == ready`;
- robots policy разрешает scope или зафиксировано полномочие владельца;
- manifest не пуст и стабилен;
- representative templates sampled;
- pagination checked;
- originals/thumbnails classified;
- invalid media либо восстановлены, либо явно разрешены как unresolved;
- recovery queue processed;
- output storage writable и достаточно места;
- rate/concurrency limits положительны;
- `USER_CONFIRMED_FULL_RUN=true` и CLI confirmation token соответствует report hash.

Последний пункт предотвращает запуск после незаметного изменения конфигурации между dry run и подтверждением.

### Ограничения не применяются

Конфигурация принимает отрицательные `MAX_PAGES`, `MAX_DEPTH` и `REQUEST_DELAY_SECONDS`. `REQUEST_DELAY_SECONDS` не используется при запросах. Нет явных limits на response size, redirects, total bytes, archive expansion и число Wayback captures.

Следует валидировать диапазоны и ввести budgets:

```yaml
MAX_PAGES: 100
MAX_DEPTH: 3
MAX_TOTAL_BYTES: 1073741824
MAX_RESPONSE_BYTES: 104857600
MAX_REDIRECTS: 5
REQUEST_DELAY_SECONDS: 2.0
RETRY_COUNT: 3
RECOVERY_CAPTURE_LIMIT: 50
```

### SSRF и redirect escape

CLI допускает произвольный `http(s)` target, включая loopback/private/link-local адреса и URLs с credentials. Allowed domains проверяются у manifest URL, но стандартный redirect handler может уйти на другой host после первоначальной проверки.

Если инструмент запускается только локально доверенным оператором, риск средний; если его вызывает агент или сервис — критический. Нужны:

- запрет userinfo в URL;
- DNS/IP policy до каждого соединения;
- блокировка loopback, private, link-local, metadata endpoints по умолчанию;
- повторная проверка host/IP после DNS и каждого redirect;
- allowlist schemes/ports;
- защита от DNS rebinding;
- отдельный opt-in для локальной сети.

## Evals и CI

Формально evals проходят, но stderr прямо сообщает: **every golden case is pending-first-green; the first rollout validates nothing until baselines are promoted**. В eval-spec все `expected` равны `null`, поэтому присутствующие `expected.json` не используются; forum expected baselines отсутствуют полностью.

Текущие validators проверяют форму отчёта, но не ключевую семантику. Поэтому evals пропускают неправильный SHA-256, отсутствие скачивания, неработающий CDX parser, неверный robots URL и пустой full run.

Немедленно:

1. Привязать каждый golden case к `expected.json` и убрать `pending-first-green`.
2. Добавить unit tests на bytes hash, invalid empty media, string/int statuses, robots rules, root URL, redirect escape, YAML edge cases и idempotency.
3. Добавить integration tests fake HTTP origin/CDX/replay.
4. Добавить negative tests, которые обязаны завершаться ненулевым кодом.
5. Запускать Linux + Windows CI, Python 3.10–3.13.
6. Добавить formatting/lint/type/security gates (`ruff`, `mypy` или pyright, `bandit`, `shellcheck`, PowerShell analyzer).
7. Добавить coverage threshold для safety-critical модулей.

`run_evals.py` использует `shell=True` для команд из eval-spec. Для доверенного репозитория это управляемо, но при запуске evals на внешнем PR или агентом содержимое spec становится исполняемым shell-кодом. Лучше хранить argv-массивы и запускать без shell.

## Документация и упаковка

### P1/P2-дефекты

- `basic-media-skill/SKILL.md` содержит mojibake в русских triggers и тексте (`СЃР°Р№С‚`, `вЂ—`-подобные последовательности). Это ухудшит естественную активацию skill и пользовательский интерфейс.
- Root README предлагает `python scripts/run_evals.py ...`, но корневого `scripts/` нет; корректные команды начинаются с `skills/<skill>/scripts/...`.
- README утверждает MIT, но LICENSE-файл отсутствует. Frontmatter не заменяет текст лицензии для нормального распространения.
- Заявление «every skill is self-contained» конфликтует с отсутствием `wayback.py` в forum skill и моделью shared helper.
- `final_report.json` создаётся даже при dry run и содержит только coverage; название вводит в заблуждение.
- Basic pipeline при повторном запуске дописывает те же строки в `data/manifest.jsonl`; проба дала 1 строку после первого запуска и 2 после второго. Нужен idempotent upsert.
- Golden `expected.json` содержит поля/пути, которых текущий report уже не выдаёт, что указывает на schema drift.
- Самописный flat-YAML parser режет комментарий по первому `#`, не поддерживает вложенные структуры и может молча проигнорировать ошибочную строку. Безопаснее JSON/TOML или PyYAML с JSON Schema.
- Нет versioned JSON Schema для config, manifest, recovery queue и reports.
- Нет changelog, release tags и migration policy.

## Сильные стороны

- Архитектурный принцип «LLM планирует, deterministic code скачивает/проверяет» выбран правильно.
- Двухфакторный full-run gate по config + CLI — хорошая основа, хотя условия пока недостаточны.
- Безопасностные границы, provenance и обязательность Wayback recovery подробно отражены в документации.
- Разделение basic/forum разумно; IPS parser заметно содержательнее обычного selector stub.
- Код компилируется, manifests валидны, Unix-installers проходят smoke test.
- Runtime-файлы и очевидные секреты в tracked tree не обнаружены.
- Fixtures компактны и пригодны как основа для настоящих regression tests.

## План исправления

### Этап 0: 24 часа

- Пометить проект `experimental`.
- Заблокировать `--run-full` с `full_run_not_implemented`.
- Добавить отсутствующую normative spec либо убрать ложные ссылки.
- Исправить mojibake и добавить `LICENSE`.
- Открыть issues на каждый P0 и включить branch protection.

### Этап 1: целостность

- Реализовать fetch-to-temp, фактический SHA-256, atomic storage и manifest upsert.
- Переписать media validation как state machine.
- Исправить robots и URL/network policy.
- Ввести versioned schemas и строгую конфигурацию.

### Этап 2: Wayback

- Исправить CDX parsing.
- Реализовать capture dedup/ranking, replay `id_`, validation и provenance.
- Реализовать fallback через archived source page.
- Закрывать recovery queue только после проверки сохранённых bytes.

### Этап 3: discovery/forum

- Реализовать bounded crawler и resume state.
- Подключить IPS extractor end-to-end.
- Добавить pagination closure, quote/reaction/attachment graph.
- Сравнить IPS adapter с независимым extractor на representative fixtures.

### Этап 4: доказуемость

- Promote реальные golden baselines.
- Добавить unit/integration/security tests и GitHub Actions.
- Исправить coverage по сущностям.
- Выпускать final report только после фактического full run и закрытия recovery queue.

## Критерии готовности

Проект можно назвать минимально готовым к реальному архивированию только когда одновременно выполнены условия:

- каждый `verified` объект имеет сохранённый файл и SHA-256 его bytes;
- повторный запуск идемпотентен и продолжает незавершённое состояние;
- robots, rate limit, redirects и network boundaries реально исполняются;
- Wayback recovery скачивает и валидирует replay, а не только создаёт очередь;
- gate невозможно открыть при `needs changes` или unresolved mandatory recovery;
- forum extractor вызывается pipeline и подтверждён golden fixtures;
- coverage считается отдельно по сущностям и pagination closure;
- CI содержит не pending, а утверждённые regression baselines;
- dry run и full run выпускают разные, семантически честные отчёты.

## Итоговая оценка

| Область | Оценка |
|---|---:|
| Идея и спецификация процесса | 8/10 |
| Документация safety boundaries | 7/10 |
| Реальная загрузка и хранение | 1/10 |
| Проверка целостности | 2/10 |
| Wayback recovery | 2/10 |
| Forum end-to-end | 3/10 |
| Тесты | 3/10 |
| CI/release engineering | 1/10 |
| Готовность к production | **2/10** |

Главная проблема не в качестве отдельных функций, а в разрыве между публично заявленным продуктом и фактически исполняемым поведением. После честной маркировки prototype, устранения пяти P0-блокеров и появления интеграционных тестов репозиторий может стать хорошей основой для специализированного архиватора.