# Деплой SedStock-web на Render (Docker)

Отдельный веб-сервис (не трогает основной `sedstock-server`). Аккаунты/подписка —
общие с приложением через `SEDSTOCK_MAIN_SERVER`.

В этой папке всё, что нужно, и НИКАКИХ секретов (ключи задаются в Render, не в коде).

## 1. Залить папку в НОВЫЙ GitHub-репозиторий
- GitHub → New repository → имя, например `SedStock-web` → Create.
- На странице репозитория: **Add file → Upload files** → перетащить ВСЁ из этой папки
  (`sedstock_web.py`, `Dockerfile`, `requirements.txt`, `.dockerignore`, `DEPLOY.md`) → Commit.
- Репозиторий можно оставить **приватным** — Render всё равно к нему подключится.

## 2. Создать Web Service на Render
- Render → **New → Web Service** → подключить этот репозиторий.
- Render сам увидит `Dockerfile` → **Runtime = Docker** (Start Command вводить НЕ нужно —
  он уже в Dockerfile).
- Plan: **Starter ($7/мес)** — как у основного сервера (Free засыпает и рвёт долгие запросы).
- **Persistent Disk НЕ нужен** (готовые ZIP временные, чистятся сами).

## 3. Environment (вкладка Environment → Add Environment Variable)
| Ключ | Значение |
|---|---|
| `AZURE_API_KEY` | *(твой ключ Azure — как на основном сервере)* |
| `AZURE_ENDPOINT` | `https://sdg0-resource.openai.azure.com` |
| `AZURE_DEPLOYMENT` | `gpt-5.4-mini` |
| `SEDSTOCK_MAIN_SERVER` | `https://sedstock-server.onrender.com` |

- `SEDSTOCK_MAIN_SERVER` **включает вход/регистрацию** (общие аккаунты с приложением).
  Если его НЕ задать — сайт пустит сразу на рабочий стол без входа (удобно только для теста).
- `PORT` **не задавать** — Render передаёт сам.
- Необязательно: `PAYPAL_ME` (по умолчанию мамин), `SEDSTOCK_ACCESS_CODE` (по умолч. `0109`),
  `SEDSTOCK_CODE_DAILY`/`SEDSTOCK_CODE_WEEKLY` (лимиты кода, 100/500).

## 4. Deploy
Create Web Service → Render соберёт Docker-образ (~2–4 мин) и выдаст адрес вида
`https://sedstock-web.onrender.com`.

## 5. Проверка
- `https://…onrender.com/health` → `{"ok": true, "service": "sedstock-web"}`
- Открыть корень `/` → экран входа (если задан `SEDSTOCK_MAIN_SERVER`).
- Войти аккаунтом из приложения или ввести код доступа `0109` → загрузить фото →
  «Анализировать» → правка метаданных → «Записать и скачать ZIP».

## 6. Отдать сайтовому Клоду
Этот адрес (`https://sedstock-web.onrender.com`) — рабочий бэкенд.
Сайт может либо просто вести на него ссылкой/редиректом, либо звать его API
(`/api/analyze`, `/api/commit`, `/api/download/<id>`, `/api/register|login|status`)
со своих страниц. UI внутри уже есть — при желании Клод заменит на брендовый.

---
**Смена ключа Azure** = поменять env `AZURE_API_KEY` на Render, код не трогать.
**Если большие пачки фото отваливаются по таймауту** — уменьшить `MAX_FILES_PER_REQUEST`
через env (по умолчанию 30) или грузить меньше за раз.
