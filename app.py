#!/usr/bin/env python3
"""Local form for office PC: login + POST /api/api/expanse_recoveries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import mimetypes
import secrets
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tzdata  # noqa: F401 — IANA zones, required on Windows
except ImportError:
    pass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DEFAULTS_PATH = ROOT / "defaults.json"
STATIC_DIR = ROOT / "static"
STATIC_PATH = STATIC_DIR / "index.html"
ADMIN_PATH = STATIC_DIR / "admin.html"
FACES_PATH = ROOT / "faces.json"
HISTORY_PATH = ROOT / "history.json"
CASHIER_PATH = ROOT / "cashier.json"
MATCH_MAX = 0.48
MATCH_GAP = 0.08

BASE_URL = "https://main.ishonchsale.uz"
SIGN_IN_URL = f"{BASE_URL}/api/api/sign-in"
SUBMIT_URL = f"{BASE_URL}/api/api/expanse_recoveries"
STAFF_URL = f"{BASE_URL}/api/api/expanse_recovery/get_staff_with_debtor_and_creditor"
COSTS_URL = f"{BASE_URL}/api/api/costs/get_costs_list_cost_code_pl"
USER_URL = f"{BASE_URL}/api/api/user"
HOST = "127.0.0.1"
PORT = 8765
try:
    TASHKENT = ZoneInfo("Asia/Tashkent")
except ZoneInfoNotFoundError:
    TASHKENT = timezone(timedelta(hours=5))
TOKEN_TTL = 20 * 60
DATA_TTL = 8 * 60

BROWSER_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ru",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/cash-register/cost-cash-register/expense-from-the-staff",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_cache_lock = threading.Lock()
_cache: dict = {"token": None, "token_at": 0.0, "staff": None, "costs": None, "data_at": 0.0}
_faces_lock = threading.Lock()
_history_lock = threading.Lock()
_cashier_lock = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl._create_unverified_context()


SSL_CONTEXT = ssl_context()


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def today_tashkent() -> str:
    return datetime.now(TASHKENT).date().isoformat()


def now_tashkent() -> str:
    return datetime.now(TASHKENT).strftime("%Y-%m-%d %H:%M")


def api_request(url: str, *, method: str, body: dict | None = None, token: str | None = None) -> tuple[int, object]:
    headers = dict(BROWSER_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is None:
        headers.pop("Content-Type", None)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {"message": str(e)}
        except json.JSONDecodeError:
            parsed = {"message": raw or str(e)}
        return e.code, parsed


def config_or_error() -> tuple[dict | None, dict | None]:
    if not CONFIG_PATH.exists():
        return None, {
            "ok": False,
            "error": "Скопируйте config.example.json в config.json и укажите логин и пароль.",
        }
    return load_json(CONFIG_PATH), None


def sign_in(config: dict) -> tuple[str | None, dict | None]:
    status, body = api_request(
        SIGN_IN_URL,
        method="POST",
        body={"username": config["username"], "password": config["password"]},
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        return None, {"ok": False, "step": "login", "status": status, "response": body}
    return body["access_token"], None


def get_token(config: dict, *, force: bool = False) -> tuple[str | None, dict | None]:
    now = time.time()
    with _cache_lock:
        if not force and _cache["token"] and now - _cache["token_at"] < TOKEN_TTL:
            return _cache["token"], None
    token, err = sign_in(config)
    if token:
        with _cache_lock:
            _cache["token"] = token
            _cache["token_at"] = time.time()
    return token, err


def staff_query(defaults: dict, page: int) -> str:
    return urllib.parse.urlencode(
        {
            "page": page,
            "search": "",
            "type": "cost",
            "cashbox_id": defaults["cashbox_id"],
            "organization_id": defaults["organization_id"],
        }
    )


def parse_staff_page(body: object) -> tuple[list[dict], int]:
    if not isinstance(body, dict):
        raise RuntimeError(f"Некорректный ответ списка сотрудников: {body}")
    people = [{"id": row["id"], "fio": row.get("fio") or ""} for row in body.get("data") or []]
    last_page = int((body.get("meta") or {}).get("last_page") or 1)
    return people, last_page


def fetch_staff(token: str, defaults: dict) -> list[dict]:
    status, body = api_request(f"{STAFF_URL}?{staff_query(defaults, 1)}", method="GET", token=token)
    if status != 200:
        raise RuntimeError(f"Не удалось загрузить сотрудников ({status}): {body}")
    people, last_page = parse_staff_page(body)
    if last_page > 1:
        with ThreadPoolExecutor(max_workers=min(4, last_page - 1)) as pool:
            futs = [
                pool.submit(
                    api_request,
                    f"{STAFF_URL}?{staff_query(defaults, page)}",
                    method="GET",
                    token=token,
                )
                for page in range(2, last_page + 1)
            ]
            for fut in futs:
                st, page_body = fut.result()
                if st != 200:
                    raise RuntimeError(f"Не удалось загрузить сотрудников ({st}): {page_body}")
                extra, _ = parse_staff_page(page_body)
                people.extend(extra)
    people.sort(key=lambda p: p["fio"])
    return people


def fetch_costs(token: str) -> list[dict]:
    status, body = api_request(COSTS_URL, method="GET", token=token)
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"Не удалось загрузить виды расхода ({status}): {body}")
    groups = []
    for group in body:
        children = []
        for child in group.get("children") or []:
            children.append(
                {
                    "id": child["id"],
                    "title": child.get("title") or "",
                    "parent_id": child.get("parent_id") or group["id"],
                    "active": child.get("active", True),
                    "cost_code_id": child.get("cost_code_id"),
                }
            )
        groups.append({"id": group["id"], "title": group.get("title") or "", "children": children})
    return groups


def load_lists(config: dict, defaults: dict, *, force: bool = False) -> tuple[str, list[dict], list[dict]]:
    now = time.time()
    with _cache_lock:
        if (
            not force
            and _cache["staff"] is not None
            and _cache["costs"] is not None
            and now - _cache["data_at"] < DATA_TTL
            and _cache["token"]
            and now - _cache["token_at"] < TOKEN_TTL
        ):
            return _cache["token"], _cache["staff"], _cache["costs"]
    token, err = get_token(config, force=force)
    if err or not token:
        raise RuntimeError((err or {}).get("error") or "Не удалось войти")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            staff_f = pool.submit(fetch_staff, token, defaults)
            costs_f = pool.submit(fetch_costs, token)
            staff = staff_f.result()
            costs = costs_f.result()
    except RuntimeError:
        if not force:
            return load_lists(config, defaults, force=True)
        raise
    with _cache_lock:
        _cache["staff"] = staff
        _cache["costs"] = costs
        _cache["data_at"] = time.time()
    return token, staff, costs


def apply_cost(payload: dict, costs: list[dict], child_id: int) -> str:
    for group in costs:
        kids = group.get("children") or []
        for child in kids:
            if int(child["id"]) == child_id:
                payload["cost_group_id"] = group["id"]
                payload["cost"]["parent_id"] = group["id"]
                payload["cost"]["child_id"] = child_id
                payload["children"] = kids
                return str(child.get("title") or "").strip()
    payload["cost"]["child_id"] = child_id
    return ""


def load_faces() -> dict:
    with _faces_lock:
        if not FACES_PATH.exists():
            return {}
        with FACES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}


def save_faces(data: dict) -> None:
    with _faces_lock:
        FACES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def descriptor_dist(a: list, b: list) -> float:
    if len(a) != len(b) or not a:
        return 999.0
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def identify_face(descriptor: list) -> dict | None:
    faces = load_faces()
    scored = []
    for sid, rec in faces.items():
        desc = rec.get("descriptors") or []
        if not desc:
            continue
        dmin = min(descriptor_dist(descriptor, d) for d in desc)
        scored.append((dmin, sid, rec))
    scored.sort(key=lambda x: x[0])
    if not scored or scored[0][0] > MATCH_MAX:
        return None
    if len(scored) > 1 and scored[1][0] - scored[0][0] < MATCH_GAP:
        return None
    rec = scored[0][2]
    return {"id": int(scored[0][1]), "fio": rec.get("fio") or ""}


def faces_public() -> list[dict]:
    out = []
    for sid, rec in load_faces().items():
        out.append(
            {
                "id": int(sid),
                "fio": rec.get("fio") or "",
                "shots": len(rec.get("descriptors") or []),
                "has_signature": bool(rec.get("signature")),
            }
        )
    out.sort(key=lambda x: x["fio"])
    return out


def load_history() -> list[dict]:
    with _history_lock:
        if not HISTORY_PATH.exists():
            return []
        with HISTORY_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []


def save_history(rows: list[dict]) -> None:
    with _history_lock:
        HISTORY_PATH.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def load_cashier() -> dict:
    with _cashier_lock:
        if not CASHIER_PATH.exists():
            return {}
        with CASHIER_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}


def save_cashier(data: dict) -> None:
    with _cashier_lock:
        CASHIER_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cashier_public() -> dict:
    rec = load_cashier()
    return {
        "ok": True,
        "fio": rec.get("fio") or "",
        "has_signature": bool(rec.get("signature")),
        "signature": rec.get("signature") or "",
    }


def valid_signature(signature: str) -> bool:
    return isinstance(signature, str) and signature.startswith("data:image/png;base64,") and 800 < len(signature) < 400_000


def decode_png(data_url: str) -> bytes | None:
    if not data_url or "base64," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split("base64,", 1)[1])
    except Exception:
        return None


def find_cyrillic_font() -> Path | None:
    for path in (
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\arialuni.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ):
        if path.exists():
            return path
    return None


def history_pdf_bytes(rows: list[dict], day: str = "") -> bytes:
    from fpdf import FPDF

    font = find_cyrillic_font()
    if not font:
        raise RuntimeError("Не найден шрифт Arial/DejaVu для кириллицы")
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.add_font("Cyr", fname=str(font))
    pdf.set_font("Cyr", size=14)
    title = "История расходов"
    if day:
        title = f"История расходов · {day}"
    pdf.cell(0, 8, title)
    pdf.ln()
    pdf.set_font("Cyr", size=8)
    pdf.cell(0, 6, now_tashkent())
    pdf.ln()
    headers = ["ПУЛ ОЛГАН ХОДИМ", "ХАРАЖАТ МАЗМУНИ", "СУММА", "ОЛДИМ ИМЗО", "БЕРДИМ ИМЗО"]
    # A4 landscape ~277mm; FIO was 70mm and left a wide gap after short names.
    widths = [50, 92, 24, 55, 55]
    pdf.set_fill_color(15, 122, 90)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(headers, widths):
        pdf.cell(w, 10, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_text_color(26, 35, 50)
    row_h = 28
    for rec in reversed(rows):
        if pdf.get_y() + row_h > 200:
            pdf.add_page()
            pdf.set_fill_color(15, 122, 90)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Cyr", size=8)
            for h, w in zip(headers, widths):
                pdf.cell(w, 10, h, border=1, fill=True, align="C")
            pdf.ln()
            pdf.set_text_color(26, 35, 50)
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_font("Cyr", size=8)
        texts = [
            " ".join(str(rec.get("staff_fio") or "").split()),
            " ".join(str(rec.get("cost_title") or "").split()),
            str(rec.get("amount") or "").strip(),
        ]
        for i, text in enumerate(texts):
            left = x + sum(widths[:i])
            pdf.rect(left, y, widths[i], row_h)
            pdf.set_xy(left + 1, y + 3)
            align = "R" if i == 2 else "L"
            pdf.multi_cell(widths[i] - 2, 4, text, align=align)
        sig_x = x + sum(widths[:3])
        pdf.rect(sig_x, y, widths[3], row_h)
        pdf.rect(sig_x + widths[3], y, widths[4], row_h)
        for i, key in enumerate(("staff_signature", "cashier_signature")):
            raw = decode_png(rec.get(key) or "")
            if not raw:
                continue
            img = io.BytesIO(raw)
            try:
                pdf.image(img, x=sig_x + i * widths[3] + 2, y=y + 2, w=widths[3] - 4, h=row_h - 4)
            except Exception:
                pass
        pdf.set_xy(x, y + row_h)
    return bytes(pdf.output())


def row_date(rec: dict) -> str:
    return str(rec.get("date") or rec.get("created_at") or "")[:10]


def history_for_date(day: str) -> list[dict]:
    day = (day or "").strip()
    if not day:
        day = today_tashkent()
    return [r for r in load_history() if row_date(r) == day]


def append_history(payload: dict) -> None:
    staff_id = str(int(payload["staff_id"]))
    face = load_faces().get(staff_id) or {}
    cashier = load_cashier()
    rows = load_history()
    rows.append(
        {
            "id": secrets.token_hex(8),
            "created_at": now_tashkent(),
            "date": payload.get("practice_day") or today_tashkent(),
            "staff_id": int(staff_id),
            "staff_fio": payload.get("staff_title") or face.get("fio") or "",
            "cost_title": payload.get("commentary") or "",
            "amount": payload.get("value") or "",
            "staff_signature": face.get("signature") or "",
            "cashier_signature": cashier.get("signature") or "",
            "cashier_name": cashier.get("fio") or "",
        }
    )
    save_history(rows)


def admin_pin_ok(pin: str) -> bool:
    config, err = config_or_error()
    if err or not config:
        return False
    expected = str(config.get("admin_pin") or "")
    return bool(expected) and secrets.compare_digest(expected, str(pin or ""))


def admin_secret() -> bytes | None:
    config, err = config_or_error()
    if err or not config:
        return None
    pin = str(config.get("admin_pin") or "")
    if not pin:
        return None
    return pin.encode("utf-8")


def new_admin_token() -> str:
    nonce = secrets.token_urlsafe(24)
    secret = admin_secret()
    if not secret:
        raise RuntimeError("Нет admin_pin в config.json")
    sig = hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{nonce}.{sig}"


def is_admin(handler: BaseHTTPRequestHandler) -> bool:
    token = handler.headers.get("X-Admin-Token") or ""
    secret = admin_secret()
    if not secret or "." not in token:
        return False
    nonce, sig = token.rsplit(".", 1)
    expected = hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return secrets.compare_digest(expected, sig)


def merge_payload(form: dict, defaults: dict, costs: list[dict]) -> dict:
    payload = json.loads(json.dumps(defaults))
    payload["value"] = "".join(ch for ch in str(form["value"]) if ch.isdigit())
    payload["staff_id"] = int(form["staff_id"])
    payload["staff_title"] = str(form["staff_title"]).strip()
    payload["practice_day"] = today_tashkent()
    title = apply_cost(payload, costs, int(form["child_id"]))
    payload["commentary"] = title or str(form.get("commentary") or "").strip()
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, status: int, body: dict | str, content_type: str = "application/json") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _serve_file(self, file: Path) -> None:
        resolved = file.resolve()
        if not str(resolved).startswith(str(STATIC_DIR.resolve())) or not resolved.is_file():
            self._send(404, {"ok": False, "error": "Not found"})
            return
        types = {
            ".js": "application/javascript; charset=utf-8",
            ".wasm": "application/wasm",
            ".json": "application/json; charset=utf-8",
            ".bin": "application/octet-stream",
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }
        ctype = types.get(resolved.suffix.lower()) or mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self._send_bytes(200, resolved.read_bytes(), ctype)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, STATIC_PATH.read_text(encoding="utf-8"), "text/html")
            return
        if path in ("/admin", "/admin.html", "/register"):
            self._send(200, ADMIN_PATH.read_text(encoding="utf-8"), "text/html")
            return
        if path in ("/manage", "/manage.html"):
            self._send(200, (STATIC_DIR / "manage.html").read_text(encoding="utf-8"), "text/html")
            return
        if path.startswith("/static/"):
            self._serve_file(STATIC_DIR / path[len("/static/") :])
            return
        if path == "/api/faces":
            if not is_admin(self):
                self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                return
            self._send(200, {"ok": True, "faces": faces_public()})
            return
        if path == "/api/history":
            if not is_admin(self):
                self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            day = (query.get("date") or [today_tashkent()])[0]
            rows = history_for_date(day)
            dates = sorted({row_date(r) for r in load_history() if row_date(r)})
            self._send(200, {"ok": True, "date": day, "dates": dates, "rows": rows})
            return
        if path == "/api/history.pdf":
            if not is_admin(self):
                self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            day = (query.get("date") or [today_tashkent()])[0]
            try:
                data = history_pdf_bytes(history_for_date(day), day)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
                return
            self._send_bytes(200, data, "application/pdf", f"history-{day}.pdf")
            return
        if path == "/api/cashier":
            if not is_admin(self):
                self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                return
            self._send(200, cashier_public())
            return
        if path == "/api/form-data":
            try:
                config, err = config_or_error()
                if err:
                    self._send(400, err)
                    return
                defaults = load_json(DEFAULTS_PATH)
                try:
                    _, staff, costs = load_lists(config, defaults)
                except RuntimeError as e:
                    token, login_err = get_token(config, force=True)
                    if login_err:
                        self._send(200, login_err)
                        return
                    if not token:
                        self._send(200, {"ok": False, "error": str(e)})
                        return
                    _, staff, costs = load_lists(config, defaults, force=True)
                self._send(
                    200,
                    {
                        "ok": True,
                        "staff": staff,
                        "costs": costs,
                        "default_child_id": defaults.get("cost", {}).get("child_id"),
                        "today": today_tashkent(),
                        "has_faces": bool(load_faces()),
                        "enrolled_ids": [int(k) for k in load_faces().keys()],
                    },
                )
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"ok": False, "error": str(e)})
            return
        self._send(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/admin/login":
                body = self._read_json()
                if not admin_pin_ok(str(body.get("pin") or "")):
                    self._send(200, {"ok": False, "error": "Неверный PIN"})
                    return
                self._send(200, {"ok": True, "token": new_admin_token()})
                return
            if path == "/api/faces":
                body = self._read_json()
                staff_id = str(int(body["staff_id"]))
                fio = str(body.get("fio") or "").strip()
                descriptors = body.get("descriptors") or []
                if not fio or len(descriptors) < 3:
                    self._send(400, {"ok": False, "error": "Нужны ФИО и 3 снимка лица"})
                    return
                signature = str(body.get("signature") or "")
                if not signature.startswith("data:image/png;base64,") or not (800 < len(signature) < 400_000):
                    self._send(400, {"ok": False, "error": "Нужна подпись"})
                    return
                faces = load_faces()
                if staff_id in faces and not is_admin(self):
                    self._send(
                        200,
                        {
                            "ok": False,
                            "error": "Этот сотрудник уже зарегистрирован. Чтобы переснять лицо, нужен администратор.",
                        },
                    )
                    return
                faces[staff_id] = {"fio": fio, "descriptors": descriptors, "signature": signature}
                save_faces(faces)
                self._send(200, {"ok": True, "faces": faces_public()})
                return
            if path == "/api/faces/delete":
                if not is_admin(self):
                    self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                    return
                body = self._read_json()
                faces = load_faces()
                faces.pop(str(int(body["staff_id"])), None)
                save_faces(faces)
                self._send(200, {"ok": True, "faces": faces_public()})
                return
            if path == "/api/cashier":
                if not is_admin(self):
                    self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                    return
                body = self._read_json()
                signature = str(body.get("signature") or "")
                if not valid_signature(signature):
                    self._send(400, {"ok": False, "error": "Нужна подпись кассира"})
                    return
                fio = str(body.get("fio") or "").strip()
                if not fio:
                    config, _err = config_or_error()
                    tok, _login_err = get_token(config) if config else (None, None)
                    if tok:
                        st, user = api_request(USER_URL, method="GET", token=tok)
                        if st == 200 and isinstance(user, dict):
                            fio = str((user.get("data") or {}).get("fullname") or "")
                    if not fio and config:
                        fio = str(config.get("username") or "Кассир")
                save_cashier({"fio": fio, "signature": signature})
                self._send(200, cashier_public())
                return
            if path == "/api/identify":
                body = self._read_json()
                person = identify_face(body.get("descriptor") or [])
                if not person:
                    self._send(200, {"ok": False, "match": None})
                    return
                self._send(200, {"ok": True, "match": person})
                return
            if path == "/api/history/delete":
                if not is_admin(self):
                    self._send(401, {"ok": False, "error": "Нужен вход администратора"})
                    return
                body = self._read_json()
                local_id = str(body.get("id") or "")
                rows = load_history()
                rec = next((r for r in rows if str(r.get("id")) == local_id), None)
                if not rec:
                    self._send(200, {"ok": False, "error": "Запись не найдена"})
                    return
                save_history([r for r in rows if str(r.get("id")) != local_id])
                day = str(body.get("date") or rec.get("date") or today_tashkent())
                self._send(200, {"ok": True, "date": day, "rows": history_for_date(day)})
                return
            if path != "/api/submit":
                self._send(404, {"ok": False, "error": "Not found"})
                return
            form = self._read_json()
            config, err = config_or_error()
            if err:
                self._send(400, err)
                return
            defaults = load_json(DEFAULTS_PATH)
            token, _, costs = load_lists(config, defaults)
            payload = merge_payload(form, defaults, costs)
            submit_status, submit_body = api_request(SUBMIT_URL, method="POST", body=payload, token=token)
            if submit_status in (401, 403):
                token, login_err = get_token(config, force=True)
                if login_err:
                    self._send(200, login_err)
                    return
                submit_status, submit_body = api_request(SUBMIT_URL, method="POST", body=payload, token=token)
            ok = 200 <= submit_status < 300
            if ok:
                try:
                    append_history(payload)
                except Exception:
                    traceback.print_exc()
            self._send(
                200,
                {
                    "ok": ok,
                    "step": "submit",
                    "status": submit_status,
                    "response": submit_body,
                },
            )
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})


class ReuseHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    if not CONFIG_PATH.exists():
        print("Нет config.json. Скопируйте config.example.json → config.json и укажите логин/пароль.")
    server = ReuseHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Касса: {url}")
    print(f"Регистрация лиц: {url}admin")
    print(f"Админ (история, PDF, подпись кассира): {url}manage")
    print("Окно можно свернуть. Чтобы остановить — закройте это окно.")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
