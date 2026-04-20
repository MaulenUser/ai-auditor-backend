# Bitrix Ingest API — Документация для фронтенда

## Оглавление

1. [Подключение](#1-подключение)
2. [Аутентификация](#2-аутентификация)
3. [Общий формат ответов](#3-общий-формат-ответов)
4. [Ошибки](#4-ошибки)
5. [Каталог — воронки и менеджеры](#5-каталог--воронки-и-менеджеры)
6. [AI Аудит](#6-ai-аудит)
7. [Ручные экспорты](#7-ручные-экспорты)
8. [Сценарий: блок "Запуск AI аудита"](#8-сценарий-блок-запуск-ai-аудита)
9. [Форматы дат](#9-форматы-дат)

---

## 1. Подключение

```
Base URL: http://localhost:8000
Swagger UI: http://localhost:8000/docs
```

Все тела запросов — **`application/x-www-form-urlencoded`** (HTML-форма), не JSON.

Проверка доступности:
```http
GET /health
```
```json
{ "status": "ok" }
```

---

## 2. Аутентификация

Три ключа передаются в **заголовках** каждого запроса. Не в теле.

| Заголовок | Для чего |
|---|---|
| `X-Webhook-Url` | CRM, звонки, каталог |
| `X-Whatsapp-Webhook-Url` | WhatsApp-экспорт, AI-аудит, preview |
| `X-OpenAI-Api-Key` | Всё что использует OpenAI (аудит, транскрипция) |

Значения берёте у заказчика. Вебхуки выглядят так:
```
https://sapaplast.bitrix24.kz/rest/1/bioc1b5xzu2usp6x/
```

**Пример с fetch:**
```js
const HEADERS = {
  "X-Webhook-Url": "https://sapaplast.bitrix24.kz/rest/1/bioc1b5xzu2usp6x/",
  "X-Whatsapp-Webhook-Url": "https://sapaplast.bitrix24.kz/rest/1/rkwh0opz6gil33ot/",
  "X-OpenAI-Api-Key": "sk-proj-...",
};

const res = await fetch("http://localhost:8000/catalog/funnels", {
  headers: HEADERS,
});
```

Храните ключи в переменных окружения или в конфиге приложения — не в коде.

---

## 3. Общий формат ответов

### GET-эндпоинты (catalog, preview)

Возвращают данные напрямую:
```json
{
  "funnels": [ ... ]
}
```

### POST-эндпоинты (экспорт, аудит)

Всегда возвращают:
```json
{
  "status": "ok",
  "data": {
    "export/audit/funnels_2/analytics/recommendations.json": { ... },
    "export/audit/funnels_2/analytics/aggregate.json": { ... }
  }
}
```

`data` — словарь, где ключ — путь к файлу на сервере, значение — содержимое файла.
Все файлы также сохраняются на диске сервера.

---

## 4. Ошибки

| HTTP-код | Причина | Что делать |
|---|---|---|
| `422` | Не передан webhook или неверные параметры | Проверить заголовки и поля формы |
| `502` | Bitrix вернул ошибку | Показать пользователю `detail` из ответа |
| `500` | Внутренняя ошибка сервера | Смотреть логи сервера |

Тело ошибки:
```json
{
  "detail": "Укажите X-Webhook-Url в Authorize."
}
```

---

## 5. Каталог — воронки и менеджеры

### GET `/catalog/funnels`

Заголовок: `X-Webhook-Url`

Запрос:
```js
const res = await fetch("http://localhost:8000/catalog/funnels", {
  headers: { "X-Webhook-Url": WEBHOOK_URL },
});
const { funnels } = await res.json();
```

Ответ:
```json
{
  "funnels": [
    { "id": "2", "name": "Окна Шымкент",               "sort": 100 },
    { "id": "4", "name": "Есиктер Агаш Темір Шымкент", "sort": 200 },
    { "id": "6", "name": "Заказы в работе",             "sort": 300 },
    { "id": "8", "name": "Алматы лидтар",               "sort": 400 }
  ]
}
```

---

### GET `/catalog/managers`

Заголовок: `X-Webhook-Url`

Запрос:
```js
const res = await fetch("http://localhost:8000/catalog/managers", {
  headers: { "X-Webhook-Url": WEBHOOK_URL },
});
const { managers } = await res.json();
```

Ответ:
```json
{
  "managers": [
    { "id": "5",  "name": "Айдана Сейтова",   "email": "a@example.com", "active": true },
    { "id": "12", "name": "Марат Ахметов",    "email": "m@example.com", "active": true },
    { "id": "18", "name": "Динара Касымова",  "email": "d@example.com", "active": true }
  ]
}
```

---

## 6. AI Аудит

### GET `/audit/preview`

Показывает **сколько данных попадёт в аудит** без запуска экспорта. Быстрый (~5-15 сек).

Заголовок: `X-Whatsapp-Webhook-Url`

Параметры передаются в URL query string:

| Параметр | Тип | Обязателен | Описание |
|---|---|---|---|
| `funnel_id` | `string[]` | нет | ID воронок. Можно несколько: `?funnel_id=2&funnel_id=4` |
| `date_from` | `string` | нет | Начало периода. Формат: `2024-01-01` |
| `date_to` | `string` | нет | Конец периода. Формат: `2024-04-30` |
| `responsible_id` | `string` | нет | ID менеджера. Пусто = весь отдел |

Запрос:
```js
const params = new URLSearchParams();
params.append("funnel_id", "2");
params.append("funnel_id", "4");
params.set("date_from", "2024-01-01");
params.set("date_to", "2024-04-30");
// params.set("responsible_id", "12"); // если конкретный менеджер

const res = await fetch(`http://localhost:8000/audit/preview?${params}`, {
  headers: { "X-Whatsapp-Webhook-Url": WA_WEBHOOK_URL },
});
const preview = await res.json();
```

Ответ:
```json
{
  "deal_count": 147,
  "manager_count": 4,
  "managers": [
    { "id": "5",  "name": "Айдана Сейтова" },
    { "id": "12", "name": "Марат Ахметов" },
    { "id": "18", "name": "Динара Касымова" },
    { "id": "23", "name": "Нурлан Жаксыбеков" }
  ],
  "funnel_count": 2,
  "funnels": [
    { "id": "2", "name": "Окна Шымкент" },
    { "id": "4", "name": "Есиктер Агаш Темір" }
  ],
  "period_from": "2024-01-01",
  "period_to": "2024-04-30",
  "actual_date_from": "2024-01-03T09:12:00+06:00",
  "actual_date_to": "2024-04-19T17:44:00+06:00",
  "total_deals_scanned": 203
}
```

---

### POST `/audit/run`

Запускает полный AI-аудит. **Долгий запрос** — 5-30 минут в зависимости от количества сделок.

Заголовки: `X-Whatsapp-Webhook-Url` + `X-OpenAI-Api-Key`

Тело — form-data (`application/x-www-form-urlencoded`):

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `funnel_id` | `string[]` | все воронки | ID воронок. Можно несколько |
| `date_from` | `string` | — | Начало периода |
| `date_to` | `string` | — | Конец периода |
| `responsible_id` | `string` | — | ID менеджера. Пусто = весь отдел |
| `limit` | `number` | `0` | Макс. сделок. `0` = без ограничений |
| `model` | `string` | `gpt-4o-mini` | OpenAI модель для анализа переписок |
| `recommendations_model` | `string` | `gpt-4o` | OpenAI модель для рекомендаций |
| `source_label` | `string` | — | Метка в отчёте, напр. `"Апрель 2024"` |

Запрос:
```js
const body = new URLSearchParams();
body.append("funnel_id", "2");
body.append("funnel_id", "4");
body.set("date_from", "2024-01-01");
body.set("date_to", "2024-04-30");
body.set("responsible_id", ""); // пусто = весь отдел
body.set("source_label", "Q1 2024 — Шымкент");

const res = await fetch("http://localhost:8000/audit/run", {
  method: "POST",
  headers: {
    "X-Whatsapp-Webhook-Url": WA_WEBHOOK_URL,
    "X-OpenAI-Api-Key": OPENAI_KEY,
    "Content-Type": "application/x-www-form-urlencoded",
  },
  body,
});
const result = await res.json();
```

Ответ:
```json
{
  "status": "ok",
  "data": {
    "export/audit/funnels_2_4/analytics/recommendations.json": {
      "generated_at": "2024-04-20T10:30:00Z",
      "source_label": "Q1 2024 — Шымкент",
      "total_items": 147,
      "model": "gpt-4o",
      "score": 6,
      "overall_assessment": "Отдел обрабатывает 77% целевых клиентов, однако...",
      "key_findings": [
        "В 55% переписок менеджер не задаёт уточняющих вопросов",
        "Самый частый запрос — замер окон (48% переписок)"
      ],
      "patterns": [
        {
          "pattern": "Менеджер сразу называет цену без выявления потребности",
          "frequency": "55% переписок",
          "impact": "negative"
        }
      ],
      "manager_insights": [
        {
          "manager_id": "12",
          "insight": "Самый высокий % квалифицированных лидов (35%)"
        }
      ],
      "recommendations": [
        {
          "title": "Ввести скрипт приветствия",
          "description": "Менеджер должен представляться по имени в первом сообщении",
          "priority": "high"
        },
        {
          "title": "Обучить задавать вопросы перед ценой",
          "description": "Минимум 3 вопроса: что именно, размеры, сроки",
          "priority": "high"
        }
      ]
    },
    "export/audit/funnels_2_4/analytics/aggregate.json": {
      "total": 147,
      "relevance_to_company": {
        "target_client":     { "count": 113, "pct": 76.9 },
        "not_target_client": { "count": 22,  "pct": 15.0 },
        "unclear":           { "count": 12,  "pct": 8.2  }
      },
      "outcome_status": {
        "follow_up":          { "count": 59, "pct": 40.1 },
        "callback_requested": { "count": 37, "pct": 25.2 },
        "qualified_interest": { "count": 29, "pct": 19.7 },
        "not_interested":     { "count": 14, "pct": 9.5  }
      },
      "per_manager": {
        "12": { "total": 45, "qualified_interest": { "pct": 35.0 } },
        "18": { "total": 38, "qualified_interest": { "pct": 18.0 } }
      }
    }
  }
}
```

---

## 7. Ручные экспорты

Эти эндпоинты нужны для пошагового запуска пайплайна вручную. Для UI-блока аудита используйте `/audit/run` — он делает всё сам.

---

### POST `/crm/export`

Заголовок: `X-Webhook-Url`

| Поле | По умолчанию | Описание |
|---|---|---|
| `date_from` | — | Начало периода |
| `date_to` | — | Конец периода |
| `skip_users` | `false` | Не экспортировать пользователей |
| `skip_activities` | `false` | Не экспортировать активности |
| `limit` | — | Макс. сделок |

---

### POST `/call-records/scan`

Заголовок: `X-Webhook-Url`

| Поле | По умолчанию | Описание |
|---|---|---|
| `date_from` | — | Начало периода |
| `date_to` | — | Конец периода |
| `limit` | `20` | Макс. звонков |
| `responsible_id` | — | Фильтр по менеджеру |

---

### POST `/recordings/download`

Заголовков не нужно.

| Поле | По умолчанию | Описание |
|---|---|---|
| `source_json` | `export/call-records-scan/recording-candidates.json` | Путь к списку записей |
| `output_dir` | `export/recordings` | Папка для аудиофайлов |
| `skip_existing` | `false` | Пропускать уже скачанные |

---

### POST `/whatsapp-timeline/export`

Заголовок: `X-Whatsapp-Webhook-Url`

| Поле | По умолчанию | Описание |
|---|---|---|
| `date_from` | — | Начало периода |
| `date_to` | — | Конец периода |
| `limit` | `100` | Макс. сделок |
| `funnel_id` | — | ID воронок (можно несколько) |
| `responsible_id` | — | Фильтр по менеджеру |
| `deal_ids` | — | Конкретные ID сделок |
| `skip_existing` | `false` | Пропускать уже экспортированные |

---

### POST `/transcribe/recordings`

Заголовок: `X-OpenAI-Api-Key`

| Поле | По умолчанию | Описание |
|---|---|---|
| `manifest_path` | `export/recordings/manifest.json` | Манифест из `/recordings/download` |
| `output_dir` | `export/transcripts` | Папка для транскриптов |
| `model` | `gpt-4o-transcribe` | Whisper-модель |
| `language` | — | Код языка (`ru`, `kk`). Пусто = авто |
| `prompt` | — | Подсказка с терминами для точности |
| `limit` | `0` | Макс. файлов |
| `skip_existing` | `false` | Пропускать уже транскрибированные |

---

### POST `/call-features/extract`

Заголовок: `X-OpenAI-Api-Key`

| Поле | По умолчанию | Описание |
|---|---|---|
| `transcript_manifest` | `export/transcripts/manifest.json` | Манифест из `/transcribe/recordings` |
| `call_metadata` | `export/call-records-scan/recording-candidates.json` | Метаданные звонков |
| `activity_metadata` | `export/call-records-scan/activities.source.json` | Данные активностей |
| `output_dir` | `export/call-features` | Папка для фич |
| `model` | `gpt-4o-mini` | OpenAI модель |
| `limit` | `0` | Макс. записей |
| `skip_existing` | `false` | Пропускать уже обработанные |

---

### POST `/analytics/aggregate`

Заголовков не нужно.

| Поле | По умолчанию | Описание |
|---|---|---|
| `features_dir` | `export/call-features/features` | Папка с feature-файлами |
| `output_dir` | `export/call-analytics` | Папка для агрегата |
| `limit` | `0` | Макс. файлов |

---

### POST `/analytics/recommendations`

Заголовок: `X-OpenAI-Api-Key`

| Поле | По умолчанию | Описание |
|---|---|---|
| `aggregate_path` | `export/call-analytics/aggregate.json` | Агрегат из `/analytics/aggregate` |
| `output_dir` | `export/call-analytics` | Папка для рекомендаций |
| `model` | `gpt-4o` | OpenAI модель |
| `source_label` | — | Метка в отчёте |

---

## 8. Сценарий: блок "Запуск AI аудита"

Полный flow для UI-блока. Три этапа: загрузка → предпросмотр → запуск.

```js
const API = "http://localhost:8000";

const HEADERS = {
  "X-Webhook-Url": "https://sapaplast.bitrix24.kz/rest/1/bioc1b5xzu2usp6x/",
  "X-Whatsapp-Webhook-Url": "https://sapaplast.bitrix24.kz/rest/1/rkwh0opz6gil33ot/",
  "X-OpenAI-Api-Key": "sk-proj-...",
};

// ── Шаг 1. Загрузить воронки и менеджеров для дропдаунов ─────────────────────

async function loadCatalog() {
  const [fRes, mRes] = await Promise.all([
    fetch(`${API}/catalog/funnels`,  { headers: HEADERS }),
    fetch(`${API}/catalog/managers`, { headers: HEADERS }),
  ]);
  const { funnels }  = await fRes.json();
  const { managers } = await mRes.json();
  return { funnels, managers };
}

// ── Шаг 2. Показать предпросмотр при изменении фильтров ──────────────────────

async function loadPreview({ funnelIds, dateFrom, dateTo, responsibleId }) {
  const params = new URLSearchParams();
  funnelIds.forEach(id => params.append("funnel_id", id));
  if (dateFrom)      params.set("date_from",      dateFrom);
  if (dateTo)        params.set("date_to",         dateTo);
  if (responsibleId) params.set("responsible_id",  responsibleId);

  const res = await fetch(`${API}/audit/preview?${params}`, {
    headers: { "X-Whatsapp-Webhook-Url": HEADERS["X-Whatsapp-Webhook-Url"] },
  });
  return res.json();
  // → { deal_count, manager_count, managers, funnels, period_from, period_to }
}

// ── Шаг 3. Запустить аудит ───────────────────────────────────────────────────

async function runAudit({ funnelIds, dateFrom, dateTo, responsibleId, label }) {
  const body = new URLSearchParams();
  funnelIds.forEach(id => body.append("funnel_id", id));
  if (dateFrom)      body.set("date_from",     dateFrom);
  if (dateTo)        body.set("date_to",        dateTo);
  if (responsibleId) body.set("responsible_id", responsibleId);
  if (label)         body.set("source_label",   label);

  const res = await fetch(`${API}/audit/run`, {
    method: "POST",
    headers: {
      "X-Whatsapp-Webhook-Url": HEADERS["X-Whatsapp-Webhook-Url"],
      "X-OpenAI-Api-Key":       HEADERS["X-OpenAI-Api-Key"],
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Ошибка сервера");
  }

  const { data } = await res.json();

  // Извлечь рекомендации из ответа
  const recKey = Object.keys(data).find(k => k.endsWith("recommendations.json"));
  const aggKey = Object.keys(data).find(k => k.endsWith("aggregate.json"));

  return {
    recommendations: recKey ? data[recKey] : null,
    aggregate:       aggKey ? data[aggKey] : null,
  };
}
```

**Что показывать пока идёт аудит:**

```js
// Запрос занимает 5-30 минут — показывайте состояния прогресса
setStatus("Загружаем переписки из Bitrix...");  // 0–30%
setStatus("GPT анализирует переписки...");       // 30–80%
setStatus("Генерируем рекомендации...");         // 80–99%

const result = await runAudit(params);
setStatus("Готово!");
showResults(result.recommendations, result.aggregate);
```

> Сервер не возвращает промежуточный прогресс — запрос завершается только когда все 4 шага готовы. Используйте анимированный спиннер или прогресс-бар с таймером.

---

## 9. Форматы дат

Все даты принимаются в формате **ISO 8601**:

```
2024-01-01          ← только дата (начало дня по UTC)
2024-04-30T23:59:59 ← дата + время
```

Bitrix возвращает даты в формате:
```
2024-04-19T17:44:00+06:00   ← с таймзоной Алматы (+06:00)
```

При отображении рекомендуется конвертировать в локальное время:
```js
const date = new Date("2024-04-19T17:44:00+06:00");
date.toLocaleDateString("ru-KZ"); // "19.04.2024"
```
