# -*- coding: utf-8 -*-
"""
SedStock ВЕБ-версия — самостоятельный Flask-модуль.

Идея: пользователь на сайте перетаскивает фото → сервер прогоняет их через
ИИ (Azure, тот же промпт что в десктопе) → exiftool ВПИСЫВАЕТ метаданные
(title/description/keywords) прямо в файлы → пользователь скачивает готовый ZIP.

Никаких скачиваний/установок/Терминала у клиента — всё в браузере.

Логика ИИ и записи метаданных перенесена 1:1 из десктопного SedStock.py,
чтобы результат был идентичным.

Запуск локально:
    pip install flask pillow requests
    set AZURE_API_KEY=...            (или в окружении Render)
    python sedstock_web.py
    открыть http://localhost:8200

Интеграция в сайт: отдать этот файл «сайтовому» Клоду. Основные точки:
    GET  /            — HTML-интерфейс (можно заменить своим дизайном)
    POST /api/process — приём фото + запись метаданных, отдаёт JSON+download_id
    GET  /api/download/<id> — скачать готовый ZIP
"""
import os
import io
import re
import json
import uuid
import time
import base64
import shutil
import zipfile
import tempfile
import datetime
import subprocess
import threading
from pathlib import Path

import requests
from PIL import Image
from flask import Flask, request, jsonify, send_file, Response

# ---------------------------------------------------------------------------
#  КОНФИГ (те же переменные окружения, что у основного сервера sedstock_server)
# ---------------------------------------------------------------------------
AZURE_API_KEY     = os.environ.get("AZURE_API_KEY", "")
AZURE_ENDPOINT    = os.environ.get("AZURE_ENDPOINT", "https://sdg0-resource.openai.azure.com")
AZURE_API_VERSION = os.environ.get("AZURE_API_VERSION", "2025-01-01-preview")
AZURE_DEPLOYMENT  = os.environ.get("AZURE_DEPLOYMENT", "gpt-5.4-mini")
AZURE_CHAT_URL    = (f"{AZURE_ENDPOINT}/openai/deployments/{AZURE_DEPLOYMENT}"
                     f"/chat/completions?api-version={AZURE_API_VERSION}")

REASONING_EFFORT      = "low"
MAX_COMPLETION_TOKENS = 4000
KW_MIN, KW_MAX        = 48, 50
KW_HARD_LIMIT         = 50
MAX_IMAGE_SIDE        = 1536

# exiftool: на сервере (Linux/Render) — системный `exiftool`; можно задать путь в env.
EXIFTOOL_BIN = os.environ.get("EXIFTOOL_BIN", "exiftool")

# Проверка подписки/регистрация через основной сервер. Пусто -> проверка ВЫКЛючена.
# Для боевого сайта: SEDSTOCK_MAIN_SERVER=https://sedstock-server.onrender.com
MAIN_SERVER_URL = os.environ.get("SEDSTOCK_MAIN_SERVER", "").rstrip("/")

# Оплата (как в приложении: PayPal.me с суммой). Для веба показываем кнопку подписки.
PAYPAL_ME       = os.environ.get("PAYPAL_ME", "https://www.paypal.me/TatsianaBartseneva")
PRICE_MONTHLY   = os.environ.get("PRICE_MONTHLY", "$4 / месяц")
PRICE_YEARLY    = os.environ.get("PRICE_YEARLY", "$30 / год")
PAYPAL_MONTHLY  = os.environ.get("PAYPAL_MONTHLY", "4USD")
PAYPAL_YEARLY   = os.environ.get("PAYPAL_YEARLY", "30USD")

MAX_FILES_PER_REQUEST = 30            # разумный предел за один заход
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

# где временно храним готовые ZIP (отдаём по download_id и чистим по TTL)
_JOBS_DIR = Path(tempfile.gettempdir()) / "sedstock_web_jobs"
_JOBS_DIR.mkdir(parents=True, exist_ok=True)
_JOB_TTL = 3600                        # секунд хранить готовый ZIP

# ---------------------------------------------------------------------------
#  КОД ДОСТУПА. Активирует доступ НАВСЕГДА, но с суточным/недельным лимитом фото.
#  «Двухюзабельный» — код может активировать не больше ACCESS_CODE_MAX человек
#  (по одному на устройство). Работает локально, НЕ трогает основной сервер —
#  это отдельный вход (например для семьи/друзей), помимо обычной подписки.
# ---------------------------------------------------------------------------
ACCESS_CODE       = os.environ.get("SEDSTOCK_ACCESS_CODE", "0109")
ACCESS_CODE_MAX   = int(os.environ.get("SEDSTOCK_ACCESS_CODE_MAX", "2"))
CODE_LIMIT_DAILY  = int(os.environ.get("SEDSTOCK_CODE_DAILY", "100"))
CODE_LIMIT_WEEKLY = int(os.environ.get("SEDSTOCK_CODE_WEEKLY", "500"))
LOCAL_PREFIX      = "sslocal:"          # префикс токена доступа по коду
_ACCESS_DB_PATH   = _JOBS_DIR / "access_codes.json"   # переживает перезапуск сервера
_ACCESS_LOCK      = threading.Lock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 МБ на запрос

# ===========================================================================
#  ПРОМПТ ДЛЯ ИИ (перенос 1:1 из SedStock.py)
# ===========================================================================
SYSTEM_PROMPT = (
    "Ты — эксперт по метаданным для микростоковых агентств "
    "(Shutterstock, Adobe Stock, iStock, Pond5). Твоя задача — по визуальному "
    "материалу составить продающие, релевантные метаданные для продажи на стоках. "
    "БАЗОВОЕ ПРАВИЛО (всегда): не выдумывай факты — если чего-то не видно на "
    "изображении или в кадре, не указывай это в title, description и keywords. "
    "Всегда отвечай ТОЛЬКО валидным JSON-объектом, без markdown-разметки, без ```-блоков, "
    "без каких-либо пояснений до или после JSON."
)
_JSON_SHAPE = (
    '{\n'
    '  "title": "продающий заголовок ровно из 10-12 слов",\n'
    '  "description": "РОВНО ОДНО предложение, описывающее содержание",\n'
    '  "keywords": ["слово", "слово", "..."],\n'
    '  "age": "возраст ЧИСЛОМ, например 35, либо пусто",\n'
    '  "is_editorial": true либо false\n'
    '}'
)
_DESC_RULE = "description: строго ОДНО предложение (не два и не больше), на английском."
_KW_RULE = (
    f"keywords: ОБЯЗАТЕЛЬНО не меньше {KW_MIN} и не больше {KW_MAX} штук, на английском, "
    "от общих понятий к частным деталям (объекты, действия, настроение/эмоция, концепция, "
    "цвета, композиция), без дубликатов. КРИТИЧЕСКИ ВАЖНО: КАЖДОЕ ключевое слово — это "
    "РОВНО ОДНО слово. НИКАКИХ словосочетаний из двух и более слов "
    "(например нельзя 'yellow flower' — только отдельно 'yellow' и 'flower')."
)
_AGE_RULE = (
    "age: если на изображении/в кадре ЕСТЬ человек — ОБЯЗАТЕЛЬНО оцени возраст "
    "главного человека и верни ТОЛЬКО ЧИСЛО (примерный возраст в годах), например "
    "\"35\" или \"7\". Без слов, без 'years', без 'лет' — только цифры. "
    "Если людей на изображении нет вообще — верни пустую строку \"\". "
    "Не дублируй возраст в массиве keywords — программа сама поставит его 5-м словом."
)
_TITLE_RULE = (
    "title: СТРОГО 10–12 слов, на английском, естественная осмысленная фраза "
    "(не набор тегов через запятую). Заголовок должен конкретно и точно описывать "
    "именно этот кадр."
)
_DISTINCT_RULE = (
    "УНИКАЛЬНОСТЬ (важно): удели особое внимание отличительным деталям именно этого "
    "кадра — выражение лица, эмоция, поза, жест, направление взгляда, ракурс, действие "
    "в моменте — и обязательно отрази их в title."
)
_SPECIFIC_RULE = (
    "КОНКРЕТИКА: если объект/растение/животное/порода/блюдо ЧЁТКО узнаваемы — назови их "
    "КОНКРЕТНО в keywords (не 'flower', а 'tulip'; не 'dog', а 'labrador'). НО если точно "
    "определить нельзя — НЕ ГАДАЙ. «Не выдумывай факты» важнее конкретики."
)
_EDITORIAL_RULE = (
    "Поле is_editorial: поставь true, если контент выглядит как РЕДАКЦИОННЫЙ — "
    "публичное место, реальное событие, узнаваемые люди/лица, бренды, логотипы, "
    "вывески, номера машин. false — если обычный commercial без узнаваемых людей и брендов. "
    "Сомневаешься — true."
)


def build_image_instruction():
    return (
        "Проанализируй это изображение и верни ТОЛЬКО JSON строго такой формы:\n"
        f"{_JSON_SHAPE}\n\n"
        f"{_TITLE_RULE}\n{_DESC_RULE}\n{_AGE_RULE}\n{_KW_RULE}\n"
        f"{_SPECIFIC_RULE}\n{_DISTINCT_RULE}\n{_EDITORIAL_RULE}"
    )


# ===========================================================================
#  РАЗБОР ОТВЕТА МОДЕЛИ (перенос 1:1)
# ===========================================================================
_KW_STOPWORDS = {
    "a", "an", "the", "of", "on", "in", "with", "and", "or", "to", "for", "at", "by",
    "is", "are", "as", "from", "into", "over", "under", "years", "year",
}


def _to_single_words(items) -> list:
    if isinstance(items, str):
        items = re.split(r"[,;\n]", items)
    out, seen = [], set()
    for it in items:
        for w in re.split(r"\s+", str(it).strip()):
            w = w.strip().strip(".,;:!?\"'()[]{}").strip()
            wl = w.lower()
            if len(w) >= 2 and wl not in _KW_STOPWORDS and wl not in seen:
                seen.add(wl)
                out.append(w)
    return out


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("Модель вернула не-JSON ответ: " + text[:200])


def _normalize_meta(raw: dict) -> dict:
    title = str(raw.get("title", "")).strip()
    description = str(raw.get("description", "")).strip()
    keywords = _to_single_words(raw.get("keywords", []))
    if not title:
        raise ValueError("В ответе модели пустой title")
    _am = re.search(r"\d{1,3}", str(raw.get("age", "")))
    age = _am.group(0) if _am else ""
    if age:
        keywords = [k for k in keywords if k != age]
        keywords.insert(min(4, len(keywords)), age)
    keywords = keywords[:KW_HARD_LIMIT]
    ed = raw.get("is_editorial", False)
    if isinstance(ed, str):
        ed = ed.strip().lower() in ("true", "1", "yes", "да", "editorial")
    return {"title": title[:200], "description": description,
            "keywords": keywords, "is_editorial": bool(ed), "age": age}


# ===========================================================================
#  ИЗОБРАЖЕНИЯ (перенос 1:1)
# ===========================================================================
def _b64_data_url(jpeg_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")


def _flatten_to_white(pil_img: Image.Image) -> Image.Image:
    if pil_img.mode in ("RGBA", "LA") or (pil_img.mode == "P" and "transparency" in pil_img.info):
        rgba = pil_img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return pil_img.convert("RGB")


def image_to_jpeg_bytes(pil_img: Image.Image, max_side=MAX_IMAGE_SIDE) -> bytes:
    """Ужатая JPEG-копия ТОЛЬКО для отправки в ИИ (в файл пишется оригинал/полный размер)."""
    img = _flatten_to_white(pil_img)
    w, h = img.size
    scale = max(w, h) / max_side
    if scale > 1:
        img = img.resize((int(w / scale), int(h / scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# ===========================================================================
#  ЗАПИСЬ МЕТАДАННЫХ ЧЕРЕЗ EXIFTOOL (перенос 1:1)
# ===========================================================================
def _run_exiftool(args) -> None:
    fd, argfile = tempfile.mkstemp(suffix=".txt", prefix="sedstock_et_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for a in args:
                f.write(a + "\n")
        cmd = [EXIFTOOL_BIN, "-charset", "filename=utf8", "-@", argfile]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=creationflags)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace")
            out = (proc.stdout or b"").decode("utf-8", "replace")
            raise RuntimeError("exiftool: " + (err.strip() or out.strip() or "неизвестная ошибка"))
    finally:
        try:
            os.unlink(argfile)
        except OSError:
            pass


def write_photo_metadata(path: Path, meta: dict) -> None:
    args = [
        "-overwrite_original",
        "-codedcharacterset=utf8",
        "-charset", "iptc=UTF8",
        f"-IPTC:ObjectName={meta['title']}",
        f"-XMP-dc:Title={meta['title']}",
        f"-IPTC:Caption-Abstract={meta['description']}",
        f"-XMP-dc:Description={meta['description']}",
        "-IPTC:Keywords=",
        "-XMP-dc:Subject=",
    ]
    for k in meta["keywords"]:
        args.append(f"-IPTC:Keywords={k}")
        args.append(f"-XMP-dc:Subject={k}")
    args.append(str(path))
    _run_exiftool(args)


def save_png_as_jpeg(png_path: Path, meta: dict) -> Path:
    """PNG стоки не читают — рядом кладём полноразмерный JPG (прозрачность→белый) и
    пишем метаданные в него. -> путь к JPG."""
    dest = png_path.with_name(png_path.stem + ".jpg")
    if dest.exists():
        dest = png_path.with_name(png_path.stem + "_stock.jpg")
    with Image.open(png_path) as im:
        im.load()
        img = _flatten_to_white(im)
    img.save(dest, "JPEG", quality=95)
    write_photo_metadata(dest, meta)
    return dest


# ===========================================================================
#  ВЫЗОВ AZURE (та же схема, что в основном сервере: retry со снятием параметров)
# ===========================================================================
MAX_CUSTOM_INSTRUCTIONS = 4000          # ограничение длины пользовательских инструкций


def _system_prompt(custom: str = "") -> str:
    """Базовый промпт + опциональные инструкции пользователя (как в десктопе).
    Базовое правило «не выдумывай факты» всегда сохраняется."""
    custom = (custom or "").strip()
    if not custom:
        return SYSTEM_PROMPT
    return (SYSTEM_PROMPT +
            "\n\nДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ОТ ПОЛЬЗОВАТЕЛЯ (учитывай их, но НЕ нарушая "
            "базовое правило «не выдумывай факты» и требования к формату JSON):\n"
            + custom[:MAX_CUSTOM_INSTRUCTIONS])


def azure_vision(jpeg_bytes: bytes, custom: str = "") -> dict:
    content = [
        {"type": "text", "text": build_image_instruction()},
        {"type": "image_url", "image_url": {"url": _b64_data_url(jpeg_bytes)}},
    ]
    body = {
        "messages": [
            {"role": "system", "content": _system_prompt(custom)},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    headers = {"api-key": AZURE_API_KEY, "Content-Type": "application/json"}
    last_err = ""
    for _ in range(4):
        r = requests.post(AZURE_CHAT_URL, headers=headers, json=body, timeout=180)
        if r.status_code == 200:
            data = r.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return _normalize_meta(_extract_json(text))
        # деплоймент не принял какой-то параметр -> убрать его и повторить
        txt = (r.text or "").lower()
        last_err = r.text[:300]
        removed = False
        for k in ("reasoning_effort", "max_completion_tokens"):
            if k in body and k.lower() in txt:
                body.pop(k, None)
                removed = True
        if not removed:
            break
    raise RuntimeError(f"Azure ошибка: {last_err}")


# ===========================================================================
#  ДОСТУП ПО КОДУ (локально, независимо от основного сервера)
#  Активации и суточное/недельное потребление храним в JSON-файле рядом с ZIP.
# ===========================================================================
def _load_access_db() -> dict:
    try:
        return json.loads(_ACCESS_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"activations": {}, "usage": {}}


def _save_access_db(db: dict) -> None:
    tmp = _ACCESS_DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_ACCESS_DB_PATH)          # атомарная замена


def _day_key() -> str:
    return datetime.date.today().isoformat()


def _week_key() -> str:
    y, w, _ = datetime.date.today().isocalendar()
    return f"{y}-W{w:02d}"


def _is_code_device(device: str) -> bool:
    if not device:
        return False
    db = _load_access_db()
    return device in db.get("activations", {}).get(ACCESS_CODE, [])


def register_code_device(device: str):
    """Регистрирует устройство на коде. -> (ok: bool, message: str)."""
    with _ACCESS_LOCK:
        db = _load_access_db()
        lst = db.setdefault("activations", {}).setdefault(ACCESS_CODE, [])
        if device in lst:
            return True, "Код уже активирован на этом устройстве."
        if len(lst) >= ACCESS_CODE_MAX:
            return False, f"Код исчерпан: его уже активировали {ACCESS_CODE_MAX} чел."
        lst.append(device)
        _save_access_db(db)
        return True, "Код активирован."


def _usage_for(device: str) -> dict:
    u = _load_access_db().get("usage", {}).get(device, {})
    dc = u.get("dc", 0) if u.get("day") == _day_key() else 0
    wc = u.get("wc", 0) if u.get("week") == _week_key() else 0
    return {"dc": dc, "wc": wc}


def code_remaining(device: str):
    """(осталось за сегодня, осталось за неделю)."""
    u = _usage_for(device)
    return (max(0, CODE_LIMIT_DAILY - u["dc"]), max(0, CODE_LIMIT_WEEKLY - u["wc"]))


def consume_code(device: str, n: int) -> None:
    if n <= 0:
        return
    with _ACCESS_LOCK:
        db = _load_access_db()
        usage = db.setdefault("usage", {})
        u = usage.get(device, {})
        dk, wk = _day_key(), _week_key()
        dc = (u.get("dc", 0) if u.get("day") == dk else 0) + n
        wc = (u.get("wc", 0) if u.get("week") == wk else 0) + n
        usage[device] = {"day": dk, "dc": dc, "week": wk, "wc": wc}
        _save_access_db(db)


def code_status(device: str) -> dict:
    """Статус в том же формате, что ждёт фронтенд (state='code')."""
    u = _usage_for(device)
    return {
        "active": True, "state": "code", "email": "Доступ по коду",
        "forever": True,
        "used": u["dc"], "quota": CODE_LIMIT_DAILY,
        "week_used": u["wc"], "week_quota": CODE_LIMIT_WEEKLY,
    }


# ===========================================================================
#  ПРОВЕРКА ДОСТУПА (подписка) через основной сервер — опционально
# ===========================================================================
def check_access(token: str) -> bool:
    # 1) доступ по коду (локальный токен) — не зависит от основного сервера
    if token and token.startswith(LOCAL_PREFIX):
        return _is_code_device(token[len(LOCAL_PREFIX):])
    # 2) обычная подписка через основной сервер
    if not MAIN_SERVER_URL:
        return True                     # проверка выключена (открытый доступ)
    if not token:
        return False
    try:
        r = requests.post(f"{MAIN_SERVER_URL}/status", json={"token": token}, timeout=15)
        st = (r.json() or {}).get("status") or {}
        return bool(st.get("active"))
    except Exception:
        return False


# ===========================================================================
#  ОБРАБОТКА ОДНОГО ФАЙЛА
# ===========================================================================
def analyze_one(src_path: Path, custom: str = "") -> dict:
    """ТОЛЬКО спрашивает ИИ (файл НЕ трогает). -> meta (title/desc/keywords/…)."""
    with Image.open(src_path) as im:
        im.load()
        jpeg_for_ai = image_to_jpeg_bytes(im)
    return azure_vision(jpeg_for_ai, custom)


def commit_one(src_path: Path, meta: dict) -> Path:
    """Вписывает (возможно, отредактированные) метаданные в файл.
    PNG -> отдельный JPG. -> путь к готовому файлу для ZIP."""
    if src_path.suffix.lower() == ".png":
        return save_png_as_jpeg(src_path, meta)          # метаданные в новый JPG
    write_photo_metadata(src_path, meta)                 # метаданные прямо в файл
    return src_path


def process_one(src_path: Path, work_dir: Path, custom: str = "") -> dict:
    """Одношаговый режим (анализ+запись сразу) — используется /api/process."""
    meta = analyze_one(src_path, custom)
    out_path = commit_one(src_path, meta)
    return {
        "filename": out_path.name,
        "title": meta["title"],
        "description": meta["description"],
        "keywords": meta["keywords"],
        "keywords_count": len(meta["keywords"]),
        "is_editorial": meta["is_editorial"],
        "out_path": out_path,
    }


def _meta_from_edit(it: dict) -> dict:
    """Санитайз отредактированных пользователем метаданных перед записью."""
    title = str(it.get("title", "")).strip() or "Untitled"
    description = str(it.get("description", "")).strip()
    keywords = _to_single_words(it.get("keywords", []))[:KW_HARD_LIMIT]
    return {"title": title[:200], "description": description,
            "keywords": keywords, "is_editorial": bool(it.get("is_editorial"))}


# --- хранилище заданий предпросмотра: оригиналы лежат до записи (по TTL) --------
def _job_dir(job_id: str) -> Path:
    return _JOBS_DIR / ("job_" + job_id)


def _read_manifest(jdir: Path) -> dict:
    try:
        return json.loads((jdir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cleanup_old_jobs():
    now = time.time()
    for p in _JOBS_DIR.glob("*.zip"):
        try:
            if now - p.stat().st_mtime > _JOB_TTL:
                p.unlink()
        except OSError:
            pass
    for d in _JOBS_DIR.glob("job_*"):           # папки с оригиналами для предпросмотра
        try:
            if d.is_dir() and now - d.stat().st_mtime > _JOB_TTL:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ===========================================================================
#  АВТОРИЗАЦИЯ — тонкие прокси к основному серверу (те же аккаунты, что в десктопе)
#  Браузер зовёт наш backend (один домен) -> он server-side дёргает основной
#  сервер. Так не нужен CORS, и все проверки подписки/триала остаются на сервере.
# ===========================================================================
def _proxy_main(path: str):
    if not MAIN_SERVER_URL:
        return jsonify({"error": "no_server",
                        "message": "SEDSTOCK_MAIN_SERVER не задан на сервере."}), 503
    try:
        r = requests.post(MAIN_SERVER_URL + path,
                          json=request.get_json(silent=True) or {}, timeout=30)
        return Response(r.content, status=r.status_code,
                        mimetype=r.headers.get("Content-Type", "application/json"))
    except Exception as e:
        return jsonify({"error": "upstream", "message": str(e)}), 502


@app.route("/api/register", methods=["POST"])
def api_register():
    return _proxy_main("/register")


@app.route("/api/login", methods=["POST"])
def api_login():
    return _proxy_main("/login")


@app.route("/api/status", methods=["POST"])
def api_status():
    # доступ по коду обслуживаем локально, обычные токены — через основной сервер
    tok = str((request.get_json(silent=True) or {}).get("token", ""))
    if tok.startswith(LOCAL_PREFIX):
        dev = tok[len(LOCAL_PREFIX):]
        return jsonify({"status": code_status(dev) if _is_code_device(dev) else None})
    return _proxy_main("/status")


@app.route("/api/start_trial", methods=["POST"])
def api_start_trial():
    return _proxy_main("/start_trial")


@app.route("/api/redeem", methods=["POST"])
def api_redeem():
    """Активация доступа по коду. Тело: {code, device_id}."""
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    device = str(body.get("device_id", "")).strip() or "web-anon"
    if code != ACCESS_CODE:
        return jsonify({"error": "bad_code", "message": "Код не подошёл."}), 400
    ok, msg = register_code_device(device)
    if not ok:
        return jsonify({"error": "code_used", "message": msg}), 409
    return jsonify({"ok": True, "token": LOCAL_PREFIX + device,
                    "status": code_status(device)})


@app.route("/api/config", methods=["GET"])
def api_config():
    """Настройки для фронтенда (цены, ссылки оплаты)."""
    return jsonify({
        "price_monthly": PRICE_MONTHLY, "price_yearly": PRICE_YEARLY,
        "paypal_monthly": PAYPAL_ME.rstrip("/") + "/" + PAYPAL_MONTHLY,
        "paypal_yearly": PAYPAL_ME.rstrip("/") + "/" + PAYPAL_YEARLY,
        "auth_enabled": bool(MAIN_SERVER_URL),
    })


# ===========================================================================
#  МАРШРУТЫ
# ===========================================================================
@app.route("/api/process", methods=["POST"])
def api_process():
    token = request.form.get("token", "") or request.headers.get("X-Token", "")
    if not check_access(token):
        return jsonify({"error": "no_access",
                        "message": "Нет активной подписки. Войдите/оформите доступ."}), 402

    files = request.files.getlist("images")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "no_files", "message": "Не выбрано ни одного фото."}), 400
    if len(files) > MAX_FILES_PER_REQUEST:
        return jsonify({"error": "too_many",
                        "message": f"За раз не больше {MAX_FILES_PER_REQUEST} фото."}), 400

    # Доступ по коду: соблюдаем суточный/недельный лимит фото.
    is_code = bool(token) and token.startswith(LOCAL_PREFIX)
    code_device = token[len(LOCAL_PREFIX):] if is_code else ""
    limit_note = ""
    if is_code:
        day_left, week_left = code_remaining(code_device)
        allowed = min(day_left, week_left)
        if allowed <= 0:
            return jsonify({"error": "limit", "message":
                            "Лимит по коду исчерпан. Осталось на сегодня: 0. "
                            "Возвращайтесь завтра."}), 429
        if len(files) > allowed:
            files = files[:allowed]
            limit_note = (f"По лимиту кода обработано только {allowed} фото "
                          f"(осталось на сегодня/неделю).")

    custom = request.form.get("instructions", "")[:MAX_CUSTOM_INSTRUCTIONS]
    _cleanup_old_jobs()
    work_dir = Path(tempfile.mkdtemp(prefix="sedstock_job_"))
    results, errors = [], []
    try:
        for f in files:
            name = os.path.basename(f.filename)
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_EXT:
                errors.append({"filename": name, "error": "Неподдерживаемый формат"})
                continue
            src = work_dir / name
            f.save(str(src))
            try:
                results.append(process_one(src, work_dir, custom))
            except Exception as e:
                errors.append({"filename": name, "error": str(e)})

        if not results:
            return jsonify({"error": "all_failed", "message": "Ни один файл не обработан.",
                            "errors": errors}), 502

        # собираем ZIP с готовыми файлами
        job_id = uuid.uuid4().hex
        zip_path = _JOBS_DIR / f"{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            for r in results:
                z.write(str(r["out_path"]), arcname=r["filename"])

        # списываем лимит кода по числу реально обработанных файлов
        if is_code:
            consume_code(code_device, len(results))

        summary = [{k: v for k, v in r.items() if k != "out_path"} for r in results]
        return jsonify({
            "ok": True,
            "processed": len(results),
            "failed": len(errors),
            "results": summary,
            "errors": errors,
            "download_id": job_id,
            "note": limit_note,
        })
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """ФАЗА 1 — ИИ читает фото, файлы НЕ трогает. Оригиналы сохраняются на
    сервере (по job_id) до записи. Возвращает метаданные для предпросмотра."""
    token = request.form.get("token", "") or request.headers.get("X-Token", "")
    if not check_access(token):
        return jsonify({"error": "no_access",
                        "message": "Нет активной подписки. Войдите/оформите доступ."}), 402

    files = request.files.getlist("images")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "no_files", "message": "Не выбрано ни одного фото."}), 400
    if len(files) > MAX_FILES_PER_REQUEST:
        return jsonify({"error": "too_many",
                        "message": f"За раз не больше {MAX_FILES_PER_REQUEST} фото."}), 400

    # доступ по коду: соблюдаем суточный/недельный лимит (списываем при анализе —
    # именно здесь тратится ИИ; сама запись бесплатна)
    is_code = bool(token) and token.startswith(LOCAL_PREFIX)
    code_device = token[len(LOCAL_PREFIX):] if is_code else ""
    limit_note = ""
    if is_code:
        day_left, week_left = code_remaining(code_device)
        allowed = min(day_left, week_left)
        if allowed <= 0:
            return jsonify({"error": "limit", "message":
                            "Лимит по коду исчерпан. Возвращайтесь завтра."}), 429
        if len(files) > allowed:
            files = files[:allowed]
            limit_note = f"По лимиту кода взято в работу только {allowed} фото."

    custom = request.form.get("instructions", "")[:MAX_CUSTOM_INSTRUCTIONS]
    _cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    jdir = _job_dir(job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    manifest = {"items": {}, "created": time.time()}
    items, errors = [], []
    for f in files:
        name = os.path.basename(f.filename)
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            errors.append({"filename": name, "error": "Неподдерживаемый формат"})
            continue
        item_id = uuid.uuid4().hex
        idir = jdir / item_id
        idir.mkdir(parents=True, exist_ok=True)
        src = idir / name
        f.save(str(src))
        try:
            meta = analyze_one(src, custom)
        except Exception as e:
            shutil.rmtree(idir, ignore_errors=True)
            errors.append({"filename": name, "error": str(e)})
            continue
        manifest["items"][item_id] = {"filename": name}
        items.append({
            "id": item_id, "filename": name,
            "title": meta["title"], "description": meta["description"],
            "keywords": meta["keywords"], "keywords_count": len(meta["keywords"]),
            "is_editorial": meta["is_editorial"], "age": meta.get("age", ""),
        })

    if not items:
        shutil.rmtree(jdir, ignore_errors=True)
        return jsonify({"error": "all_failed", "message": "Ни один файл не распознан.",
                        "errors": errors}), 502

    (jdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                        encoding="utf-8")
    if is_code:
        consume_code(code_device, len(items))

    return jsonify({"ok": True, "job_id": job_id, "items": items,
                    "errors": errors, "note": limit_note})


@app.route("/api/commit", methods=["POST"])
def api_commit():
    """ФАЗА 2 — пишет (возможно отредактированные) метаданные в оригиналы и
    собирает ZIP. Тело: {job_id, items:[{id,title,description,keywords,is_editorial}]}."""
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("job_id", ""))
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"error": "bad_job", "message": "Неверный идентификатор задания."}), 400
    jdir = _job_dir(job_id)
    manifest = _read_manifest(jdir)
    if not manifest.get("items"):
        return jsonify({"error": "expired",
                        "message": "Задание не найдено (истёк срок). Загрузите фото заново."}), 404

    edits = body.get("items") or []
    results, errors = [], []
    seen_names = {}
    for it in edits:
        item_id = str(it.get("id", ""))
        rec = manifest["items"].get(item_id)
        if not rec:
            continue
        name = rec["filename"]
        src = jdir / item_id / name
        if not src.exists():
            errors.append({"filename": name, "error": "Оригинал не найден"})
            continue
        try:
            out_path = commit_one(src, _meta_from_edit(it))
        except Exception as e:
            errors.append({"filename": name, "error": str(e)})
            continue
        # уникальное имя в архиве (на случай одинаковых имён файлов)
        arc = out_path.name
        if arc in seen_names:
            seen_names[arc] += 1
            arc = f"{Path(arc).stem}_{seen_names[arc]}{Path(arc).suffix}"
        else:
            seen_names[arc] = 0
        results.append((out_path, arc))

    if not results:
        return jsonify({"error": "nothing", "message": "Нечего записывать.",
                        "errors": errors}), 400

    job_out = uuid.uuid4().hex
    zip_path = _JOBS_DIR / f"{job_out}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for out_path, arc in results:
            z.write(str(out_path), arcname=arc)

    shutil.rmtree(jdir, ignore_errors=True)          # оригиналы больше не нужны
    return jsonify({"ok": True, "processed": len(results), "failed": len(errors),
                    "errors": errors, "download_id": job_out})


@app.route("/api/download/<job_id>", methods=["GET"])
def api_download(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return "bad id", 400
    zip_path = _JOBS_DIR / f"{job_id}.zip"
    if not zip_path.exists():
        return "not found (возможно, срок хранения истёк)", 404
    return send_file(str(zip_path), as_attachment=True,
                     download_name="SedStock-готовые-фото.zip",
                     mimetype="application/zip")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "sedstock-web"})


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


# ===========================================================================
#  ИНТЕРФЕЙС (простой, самодостаточный; сайтовый Клод заменит на ваш дизайн)
# ===========================================================================
INDEX_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SedStock — веб</title>
<style>
  :root{ --bg:#f5f5f7; --card:#fff; --accent:#0071e3; --text:#1d1d1f; --muted:#86868b; --border:#e3e3e8; }
  *{box-sizing:border-box} body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
    background:var(--bg);color:var(--text)}
  .wrap{max-width:900px;margin:0 auto;padding:32px 20px}
  h1{font-size:28px;margin:0 0 4px} .sub{color:var(--muted);margin:0 0 24px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:24px;margin-bottom:18px}
  .drop{border:2px dashed var(--border);border-radius:16px;padding:40px;text-align:center;cursor:pointer;
    transition:.15s} .drop.over{border-color:var(--accent);background:#f0f7ff}
  .drop b{color:var(--accent)}
  .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
  input[type=text]{flex:1;min-width:140px;padding:10px 12px;border:1px solid var(--border);border-radius:10px;font-size:14px}
  button{background:var(--accent);color:#fff;border:0;border-radius:12px;padding:12px 22px;font-size:15px;
    font-weight:600;cursor:pointer} button:disabled{opacity:.5;cursor:default}
  .files{margin-top:14px;font-size:13px;color:var(--muted)}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--border);vertical-align:top}
  th{color:var(--muted);font-weight:600}
  .tag{display:inline-block;background:#eef;border-radius:6px;padding:1px 6px;margin:1px;font-size:11px}
  .bar{height:8px;background:#eee;border-radius:4px;overflow:hidden;margin-top:12px}
  .bar > i{display:block;height:100%;background:var(--accent);width:0;transition:.3s}
  .dl{display:inline-block;margin-top:14px;background:#34c759}
  .err{color:#c00;font-size:13px}
  .ok{color:#2a9d2a}
  [hidden]{display:none!important}
  .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
  .chip{font-size:13px;color:var(--muted);display:flex;gap:12px;align-items:center}
  .chip a{color:var(--accent);cursor:pointer;text-decoration:none}
  .auth{max-width:420px;margin:6vh auto 0}
  .seg{display:flex;background:#e8e8ed;border-radius:10px;padding:3px;margin:0 0 18px}
  .seg button{flex:1;background:transparent;color:var(--text);border-radius:8px;padding:9px;font-size:14px}
  .seg button.on{background:#fff}
  .field{width:100%;padding:12px;border:1px solid var(--border);border-radius:10px;font-size:15px;margin-bottom:10px}
  .btnwide{width:100%;height:48px;font-size:16px}
  .ghost{background:#eef2f7;color:var(--text)}
  .green{background:#34c759}
  .muted{color:var(--muted);font-size:13px;text-align:center;margin-top:12px}
  .plan{background:#f5f8ff;border:2px solid var(--accent);border-radius:16px;padding:18px;margin-bottom:12px}
  .reshead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
  .reshead b{font-size:15px}
  .pcard{border:1px solid var(--border);border-radius:14px;padding:14px;margin-bottom:12px;background:#fbfbfd}
  .pcard .ph{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}
  .pcard .fn{font-weight:600;font-size:14px;word-break:break-all}
  .pcard label{display:block;font-size:12px;color:var(--muted);margin:8px 0 3px}
  .pcard input,.pcard textarea{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:9px;
    font-size:14px;font-family:inherit;background:#fff}
  .pcard textarea{resize:vertical;min-height:56px;line-height:1.4}
  .kwc{color:var(--muted);font-weight:400}
  .edbadge{background:var(--border);color:var(--muted);border:0;border-radius:8px;padding:5px 12px;
    font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
  .edbadge.on{background:#ff9500;color:#fff}
  .pcard.err{background:#fff5f5;border-color:#f3c2c2}
</style></head>
<body><div class="wrap">

  <!-- ЭКРАН ВХОДА / РЕГИСТРАЦИИ (как в приложении) -->
  <div id="authView" class="auth" hidden>
    <h1 style="text-align:center;margin-bottom:2px">SedStock</h1>
    <p class="sub" style="text-align:center">ИИ-разметка фото для микростоков</p>
    <div class="card">
      <div class="seg">
        <button id="segLogin" class="on" onclick="setSeg('login')">Вход</button>
        <button id="segReg" onclick="setSeg('register')">Регистрация</button>
      </div>
      <input id="aEmail" class="field" type="email" placeholder="Email" autocomplete="email">
      <input id="aPass" class="field" type="password" placeholder="Пароль" autocomplete="current-password">
      <input id="aCode" class="field" type="text" placeholder="Код приглашения (если есть)" hidden>
      <div id="authErr" class="err" style="margin-bottom:8px"></div>
      <button id="authBtn" class="btnwide" onclick="doAuth()">Войти</button>
      <div id="regHint" class="muted" hidden>🎁 После регистрации — 3 дня бесплатно</div>
      <div class="muted" style="margin-top:14px"><a onclick="toggleCode()">У меня есть код доступа</a></div>
      <div id="codeBox" hidden style="margin-top:10px">
        <input id="accessCode" class="field" type="text" placeholder="Код доступа" autocomplete="off">
        <button class="btnwide green" onclick="redeemCode()">Активировать</button>
        <div id="codeErr" class="err" style="margin-top:6px"></div>
      </div>
    </div>
  </div>

  <!-- ЭКРАН ПЛАНА / ПОДПИСКИ -->
  <div id="planView" hidden>
    <div class="top"><h1 style="margin:0">Выберите план</h1>
      <div class="chip"><span id="planEmail"></span><a onclick="logout()">Выйти</a></div></div>
    <div class="card">
      <div class="plan">
        <b style="font-size:18px">SedStock PRO</b>
        <div class="muted" style="text-align:left;margin:6px 0 12px">• 100 фото в день, 500 в неделю · Фото и видео</div>
        <button class="btnwide" onclick="subscribe('monthly')">Подписка — <span id="pMonthly"></span></button>
        <button class="btnwide green" style="margin-top:8px" onclick="subscribe('yearly')">Год — <span id="pYearly"></span> (выгоднее)</button>
      </div>
      <button id="trialBtn" class="btnwide ghost" onclick="startTrial()">Начать 3 дня бесплатно</button>
      <div id="planErr" class="err" style="margin-top:8px"></div>
      <button class="btnwide ghost" style="margin-top:8px" onclick="recheck()">Я оплатил — проверить</button>
    </div>
  </div>

  <!-- РАБОЧИЙ СТОЛ -->
  <div id="mainView" hidden>
    <div class="top">
      <div><h1 style="margin:0">SedStock</h1><p class="sub" style="margin:0">ИИ-разметка фото для стоков</p></div>
      <div class="chip"><span id="chip"></span><a onclick="logout()">Выйти</a></div>
    </div>
    <details class="card" id="instrCard">
      <summary style="cursor:pointer;font-weight:600;list-style:none">⚙️ Инструкции для ИИ <span class="muted" style="font-weight:400">— необязательно, если не нравится стиль разметки</span></summary>
      <p class="muted" style="text-align:left;margin:10px 0 6px">Ваши пожелания к разметке: стиль заголовков, что подчёркивать, какие слова не использовать. Базовое правило «не выдумывать факты» и формат сохраняются. Влияет на заголовок, описание и ключевые слова.</p>
      <textarea id="aiInstr" class="field" style="min-height:80px;resize:vertical" placeholder="Например: заголовки в деловом стиле; добавляй настроение в ключевые слова; не используй слово «person»."></textarea>
      <div class="muted" id="instrSaved" style="text-align:left;min-height:16px"></div>
    </details>
    <div class="card">
      <div id="drop" class="drop">
        Перетащите фото сюда или <b>нажмите, чтобы выбрать</b><br>
        <small style="color:var(--muted)">JPG, PNG, WEBP, TIFF · до 30 за раз</small>
        <input id="file" type="file" accept="image/*" multiple hidden>
      </div>
      <div class="files" id="files"></div>
      <div class="row"><button id="go" disabled>Анализировать</button></div>
      <div class="bar" id="barWrap" hidden><i id="bar"></i></div>
      <div id="status" class="files"></div>
    </div>
    <div class="card" id="resCard" hidden>
      <div class="reshead">
        <b>Проверьте метаданные и при желании отредактируйте</b>
        <button id="commit" class="green">⬇ Записать и скачать ZIP</button>
      </div>
      <div id="preview"></div>
      <div id="errs" class="err"></div>
    </div>
  </div>

<script>
const $ = s => document.querySelector(s);
let CFG = {}, seg = "login", chosen = [];

function getToken(){ try{return localStorage.getItem("ss_token")||"";}catch(e){return "";} }
function setToken(t){ try{localStorage.setItem("ss_token",t);}catch(e){} }
function clearToken(){ try{localStorage.removeItem("ss_token");}catch(e){} }
function getDevice(){ try{ let d=localStorage.getItem("ss_device"); if(!d){ d="web-"+(crypto.randomUUID?crypto.randomUUID():Math.random().toString(36).slice(2)); localStorage.setItem("ss_device",d);} return d; }catch(e){ return "web-anon"; } }
function getInstr(){ try{return localStorage.getItem("ss_instr")||"";}catch(e){return "";} }
function setInstr(v){ try{localStorage.setItem("ss_instr",v);}catch(e){} }

function show(id){ ["authView","planView","mainView"].forEach(v=>$("#"+v).hidden=(v!==id)); }

async function api(path, body){
  const r = await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  let j={}; try{ j=await r.json(); }catch(e){}
  return {ok:r.ok, code:r.status, data:j};
}

(async function boot(){
  try{ CFG = await (await fetch("/api/config")).json(); }catch(e){ CFG={}; }
  $("#pMonthly").textContent = CFG.price_monthly||"$4 / месяц";
  $("#pYearly").textContent  = CFG.price_yearly||"$30 / год";
  const ai=$("#aiInstr"); if(ai){ ai.value=getInstr();
    let t; ai.addEventListener("input",()=>{ setInstr(ai.value); clearTimeout(t);
      $("#instrSaved").textContent="Сохранено ✓"; t=setTimeout(()=>{$("#instrSaved").textContent="";},1500); }); }
  if(CFG.auth_enabled === false){ show("mainView"); return; }
  const tok = getToken();
  if(!tok){ show("authView"); return; }
  const r = await api("/api/status",{token:tok});
  routeByStatus((r.data||{}).status);
})();

function routeByStatus(st){
  if(st && st.active){ show("mainView"); renderChip(st); }
  else if(st){ show("planView"); $("#planEmail").textContent=st.email||""; $("#trialBtn").hidden=(st.state!=="none"); }
  else { show("authView"); }
}

function setSeg(s){ seg=s; $("#segLogin").classList.toggle("on",s==="login"); $("#segReg").classList.toggle("on",s==="register");
  $("#aCode").hidden=(s!=="register"); $("#regHint").hidden=(s!=="register");
  $("#authBtn").textContent=s==="login"?"Войти":"Создать аккаунт"; $("#authErr").textContent=""; }

function toggleCode(){ $("#codeBox").hidden=!$("#codeBox").hidden; }
async function redeemCode(){
  const code=$("#accessCode").value.trim();
  if(!code){ $("#codeErr").textContent="Введите код."; return; }
  $("#codeErr").textContent="Проверяю код…";
  const r=await api("/api/redeem",{code, device_id:getDevice()});
  if(r.ok && r.data.token){ $("#codeErr").textContent=""; setToken(r.data.token); routeByStatus(r.data.status); }
  else { $("#codeErr").textContent=(r.data.message||"Код не подошёл."); }
}

async function doAuth(){
  const email=$("#aEmail").value.trim(), password=$("#aPass").value, code=$("#aCode").value.trim();
  if(!email||!password){ $("#authErr").textContent="Введите email и пароль."; return; }
  $("#authBtn").disabled=true; $("#authErr").textContent="Подождите…";
  const r = await api(seg==="login"?"/api/login":"/api/register",{email,password,code,device_id:getDevice()});
  $("#authBtn").disabled=false;
  if(!r.ok){ $("#authErr").textContent=(r.data.message||r.data.error||"Не удалось. Проверьте данные."); return; }
  $("#authErr").textContent=""; setToken(r.data.token); routeByStatus(r.data.status);
}

async function startTrial(){
  $("#planErr").textContent="Включаю пробный период…";
  const r = await api("/api/start_trial",{token:getToken()});
  if(r.ok && r.data.status){ routeByStatus(r.data.status); }
  else { $("#planErr").textContent=(r.data.message||r.data.error||"Не удалось начать триал."); }
}
function subscribe(period){
  const url = period==="yearly"?CFG.paypal_yearly:CFG.paypal_monthly;
  if(url) window.open(url,"_blank");
  $("#planErr").textContent="Открыл оплату в PayPal. После оплаты нажмите «Я оплатил — проверить».";
}
async function recheck(){
  $("#planErr").textContent="Проверяю оплату…";
  const r = await api("/api/status",{token:getToken()}); const st=(r.data||{}).status;
  if(st && st.active){ routeByStatus(st); } else { $("#planErr").textContent="Оплата пока не подтверждена. Подождите минуту и нажмите ещё раз."; }
}

function renderChip(st){
  let t="Нет плана";
  if(st.state==="unlimited") t="Безлимит";
  else if(st.state==="code") t="Доступ по коду · "+(st.used||0)+"/"+(st.quota||0)+" сегодня";
  else if(st.state==="paid") t="Подписка · "+(st.days_left||0)+" дн.";
  else if(st.state==="trial") t="Пробный · "+(st.days_left||0)+" дн. · "+(st.used||0)+"/"+(st.quota||0);
  $("#chip").textContent=t;
}
function logout(){ clearToken(); location.reload(); }

const drop=$("#drop"), file=$("#file"), go=$("#go"), commit=$("#commit");
let jobId=null;
drop.onclick=()=>file.click();
["dragover","dragenter"].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add("over");}));
["dragleave","drop"].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove("over");}));
drop.addEventListener("drop",ev=>addFiles(ev.dataTransfer.files));
file.onchange=()=>addFiles(file.files);
function addFiles(list){ chosen=Array.from(list).slice(0,30); $("#files").textContent=chosen.length?`Выбрано файлов: ${chosen.length}`:""; go.disabled=chosen.length===0; }

function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

// ФАЗА 1 — анализ (ИИ читает фото, файлы не трогаются)
go.onclick=async()=>{
  if(!chosen.length) return;
  go.disabled=true; $("#barWrap").hidden=false; $("#bar").style.width="15%";
  $("#status").textContent="Анализирую… ИИ читает фото (файлы пока не меняются).";
  $("#resCard").hidden=true; jobId=null;
  const fd=new FormData(); chosen.forEach(f=>fd.append("images",f)); fd.append("token",getToken());
  fd.append("instructions",getInstr());
  try{
    const r=await fetch("/api/analyze",{method:"POST",body:fd});
    $("#bar").style.width="90%";
    if(r.status===402){ const s=await api("/api/status",{token:getToken()}); routeByStatus((s.data||{}).status); return; }
    const data=await r.json();
    if(r.status===429){ $("#status").innerHTML=`<span class="err">${esc(data.message||"Лимит исчерпан.")}</span>`; go.disabled=false; $("#barWrap").hidden=true; return; }
    if(!data.ok){ $("#status").innerHTML=`<span class="err">${esc(data.message||"Ошибка")}</span>`; go.disabled=false; return; }
    $("#bar").style.width="100%"; jobId=data.job_id;
    const note=data.note?` <span class="err">${esc(data.note)}</span>`:"";
    $("#status").innerHTML=`<span class="ok">Готово: распознано ${data.items.length}, ошибок ${(data.errors||[]).length}. Проверьте и жмите «Записать и скачать».</span>${note}`;
    renderPreview(data);
    $("#resCard").hidden=false;
    const s=await api("/api/status",{token:getToken()}); if((s.data||{}).status) renderChip(s.data.status);
  }catch(e){ $("#status").innerHTML=`<span class="err">Сбой сети: ${esc(e)}</span>`; }
  go.disabled=false;
};

function renderPreview(data){
  const box=$("#preview"); box.innerHTML="";
  (data.items||[]).forEach(x=>{
    const kw=(x.keywords||[]).join(", ");
    const on=x.is_editorial?"on":"";
    box.insertAdjacentHTML("beforeend",
      `<div class="pcard" data-id="${esc(x.id)}" data-ed="${x.is_editorial?1:0}">
         <div class="ph"><span class="fn">${esc(x.filename)}</span>
           <button class="edbadge ${on}" data-role="ed" type="button">Editorial</button></div>
         <label>Заголовок</label><input class="p-title" value="${esc(x.title)}">
         <label>Описание</label><input class="p-desc" value="${esc(x.description)}">
         <label>Ключевые слова через запятую <span class="kwc">(${(x.keywords||[]).length})</span></label>
         <textarea class="p-kw">${esc(kw)}</textarea>
       </div>`);
  });
  (data.errors||[]).forEach(e=>{
    box.insertAdjacentHTML("beforeend",
      `<div class="pcard err"><span class="fn">${esc(e.filename)}</span> — ${esc(e.error)}</div>`);
  });
  $("#errs").innerHTML="";
}

// делегирование: тумблер Editorial + счётчик ключевых слов
$("#preview").addEventListener("click",ev=>{
  const b=ev.target.closest('[data-role="ed"]'); if(!b) return;
  const c=b.closest(".pcard"); const on=c.dataset.ed==="1"?"0":"1";
  c.dataset.ed=on; b.classList.toggle("on",on==="1");
});
$("#preview").addEventListener("input",ev=>{
  if(!ev.target.classList.contains("p-kw")) return;
  const n=ev.target.value.split(",").map(s=>s.trim()).filter(Boolean).length;
  const el=ev.target.closest(".pcard").querySelector(".kwc"); if(el) el.textContent=`(${n})`;
});

// ФАЗА 2 — запись отредактированных метаданных + скачивание ZIP
commit.onclick=async()=>{
  if(!jobId) return;
  const items=[];
  document.querySelectorAll("#preview .pcard:not(.err)").forEach(c=>{
    items.push({ id:c.dataset.id,
      title:c.querySelector(".p-title").value,
      description:c.querySelector(".p-desc").value,
      keywords:c.querySelector(".p-kw").value,
      is_editorial:c.dataset.ed==="1" });
  });
  if(!items.length) return;
  commit.disabled=true; $("#status").innerHTML="Записываю метаданные в файлы…";
  const r=await api("/api/commit",{job_id:jobId, token:getToken(), items});
  commit.disabled=false;
  if(!r.ok || !r.data.ok){ $("#status").innerHTML=`<span class="err">${esc((r.data&&r.data.message)||"Не удалось записать.")}</span>`; return; }
  $("#status").innerHTML=`<span class="ok">Записано ${r.data.processed}. Скачивание началось…</span>`;
  $("#errs").innerHTML=((r.data.errors)||[]).map(e=>`${esc(e.filename)}: ${esc(e.error)}`).join("<br>");
  window.location.href="/api/download/"+r.data.download_id;   // ZIP отдаётся как файл
  jobId=null; $("#resCard").hidden=true; chosen=[]; $("#files").textContent=""; go.disabled=true;
};
</script>
</div></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", "8200"))
    app.run(host="0.0.0.0", port=port, debug=False)
