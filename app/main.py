"""Telegram link -> server downloader.

Paste a t.me message link, pick a folder under DOWNLOAD_ROOT, and the media is
downloaded straight to the server through your own Telegram account (Telethon).
"""
import asyncio
import base64
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from telethon import TelegramClient, errors
from telethon.tl.types import PeerChannel

DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
STATIC = Path(__file__).parent / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"
CONFIG_FILE = DATA_DIR / "config.json"


def api_creds():
    """API_ID/API_HASH from the environment, or from the setup screen (config.json)."""
    api_id, api_hash = os.environ.get("API_ID", ""), os.environ.get("API_HASH", "")
    if not (api_id and api_hash):
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            api_id, api_hash = cfg["api_id"], cfg["api_hash"]
        except Exception:
            return None
    try:
        return int(api_id), str(api_hash)
    except ValueError:
        return None

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
security = HTTPBasic(auto_error=False)


def check_auth(creds: Optional[HTTPBasicCredentials] = Depends(security)):
    if not APP_PASSWORD:
        return
    if creds is None or not secrets.compare_digest(creds.password.encode(), APP_PASSWORD.encode()):
        raise HTTPException(401, "auth", headers={"WWW-Authenticate": "Basic"})


client: Optional[TelegramClient] = None
login_state: Dict[str, str] = {}
jobs: Dict[str, dict] = {}
tasks: Dict[str, asyncio.Task] = {}
semaphore = asyncio.Semaphore(MAX_PARALLEL)


# ---------- helpers ----------

def load_history():
    try:
        for j in json.loads(HISTORY_FILE.read_text()):
            if j.get("status") in ("queued", "downloading"):
                j["status"] = "error"
                j["error"] = "הופסק (השרת הופעל מחדש)"
            jobs[j["id"]] = j
    except Exception:
        pass


def save_history():
    items = sorted(jobs.values(), key=lambda j: j["created"])[-200:]
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False))
    tmp.replace(HISTORY_FILE)


def safe_dir(rel: str) -> Path:
    p = (DOWNLOAD_ROOT / (rel or "").strip("/")).resolve()
    if p != DOWNLOAD_ROOT and DOWNLOAD_ROOT not in p.parents:
        raise HTTPException(400, "תיקייה לא חוקית")
    return p


def rel_of(p: Path) -> str:
    r = str(p.relative_to(DOWNLOAD_ROOT))
    return "" if r == "." else r


LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(c/)?([A-Za-z0-9_]+)/(\d+)(?:/(\d+))?"
)


def parse_link(link: str):
    m = LINK_RE.search(link.strip())
    if not m:
        raise ValueError("זה לא נראה כמו קישור להודעה בטלגרם (t.me/ערוץ/מספר)")
    private, chat, a, b = m.groups()
    msg_id = int(b or a)  # topic links: t.me/chat/<topic>/<msg>
    if private:
        return PeerChannel(int(chat)), msg_id, f"c{chat}"
    return chat, msg_id, chat


def clean_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "file"


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists() or p.with_name(p.name + ".part").exists():
        p = folder / f"{stem} ({i}){suf}"
        i += 1
    return p


SEASON_RE = re.compile(r"(?:עונה|season)\s*[:#]?\s*(\d{1,3})", re.I)
EPISODE_RE = re.compile(r"(?:פרק|episode|ep\.?)\s*[:#]?\s*(\d{1,4})", re.I)
SXE_RE = re.compile(r"\bS(\d{1,3})\s*[\.\-_ ]?\s*E(\d{1,4})\b", re.I)
RANDOM_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")


def looks_random(stem: str) -> bool:
    """Telegram re-uploads often carry names like 'PEJxpJGh' - no words, mixed case/digits."""
    if not RANDOM_RE.match(stem) or SXE_RE.search(stem):
        return False
    kinds = sum(bool(re.search(r, stem)) for r in (r"[a-z]", r"[A-Z]", r"\d"))
    return kinds >= 2 and not re.search(r"[\-_]", stem) and not re.search(r"[a-z]{4}", stem)


def episode_name(caption: str, fallback_series: str = ""):
    """'בודקת • עונה 1, פרק 6' -> 'בודקת - עונה 1 פרק 6 - S01E06'. None when no episode number."""
    text = caption or ""
    sxe = SXE_RE.search(text)
    se, ep = SEASON_RE.search(text), EPISODE_RE.search(text)
    season = int(sxe.group(1)) if sxe else int(se.group(1)) if se else None
    episode = int(sxe.group(2)) if sxe else int(ep.group(1)) if ep else None
    if episode is None:
        return None
    first = next((l for l in text.splitlines() if l.strip()), "")
    cut = min([m.start() for m in (SXE_RE.search(first), SEASON_RE.search(first), EPISODE_RE.search(first)) if m]
              or [len(first)])
    series = re.sub(r"[^\w\s'\"׳״.&!?()-]", " ", first[:cut], flags=re.U)
    series = re.sub(r"\s+", " ", series).strip(" -–—|,:.•")
    series = series or fallback_series
    season = season or 1
    parts = [series] if series else []
    parts.append(f"עונה {season} פרק {episode}")
    parts.append(f"S{season:02d}E{episode:02d}")
    return clean_name(" - ".join(parts))


def media_name(msg, label: str, folder_name: str = "") -> str:
    f = msg.file
    ext = (f.ext if f else None) or (Path(f.name).suffix if f and f.name else "") or ".bin"
    caption = msg.message or ""
    ep = episode_name(caption, folder_name)
    if ep:
        return ep + ext
    if f and f.name and not (caption.strip() and looks_random(Path(f.name).stem)):
        return clean_name(f.name)
    title = clean_name(caption.splitlines()[0])[:120] if caption.strip() else ""
    return (title or f"{label}_{msg.id}") + ext


def kind_of(msg) -> str:
    if msg.video or msg.gif:
        return "video"
    if msg.photo:
        return "photo"
    if msg.audio or msg.voice:
        return "audio"
    return "file"


async def thumb_data(msg) -> Optional[str]:
    try:
        data = await client.download_media(msg, thumb=-1, file=bytes)
        if data:
            return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    except Exception:
        pass
    return None


async def get_client() -> TelegramClient:
    global client
    creds = api_creds()
    if not creds:
        raise HTTPException(409, "צריך להגדיר API_ID ו־API_HASH")
    if client is None:
        client = TelegramClient(str(DATA_DIR / "tg"), creds[0], creds[1],
                                device_model="TG Downloader", system_version="Docker",
                                app_version="1.0")
    if not client.is_connected():
        await client.connect()
    return client


async def require_login() -> TelegramClient:
    c = await get_client()
    if not await c.is_user_authorized():
        raise HTTPException(401, "צריך להתחבר לטלגרם")
    return c


async def resolve_chat(c: TelegramClient, chat):
    try:
        return await c.get_input_entity(chat)
    except (ValueError, TypeError):
        if isinstance(chat, PeerChannel):
            await c.get_dialogs()  # fill the entity cache for private chats
            return await c.get_input_entity(chat)
        raise


# ---------- download worker ----------

async def run_job(job_id: str, msg, target: Path):
    job = jobs[job_id]
    part = target.with_name(target.name + ".part")
    last = [time.time(), 0]

    def progress(cur, total):
        now = time.time()
        job["done"], job["size"] = cur, total or job["size"]
        if now - last[0] >= 0.7:
            job["speed"] = (cur - last[1]) / (now - last[0])
            last[0], last[1] = now, cur

    try:
        async with semaphore:
            job.update(status="downloading", started=time.time())
            await client.download_media(msg, file=str(part), progress_callback=progress)
            part.replace(target)
            job.update(status="done", done=job["size"], speed=0, finished=time.time())
    except asyncio.CancelledError:
        part.unlink(missing_ok=True)
        job.update(status="cancelled", speed=0)
    except Exception as e:  # noqa: BLE001
        part.unlink(missing_ok=True)
        job.update(status="error", error=str(e) or type(e).__name__, speed=0)
    finally:
        tasks.pop(job_id, None)
        save_history()


# ---------- API ----------

class PhoneIn(BaseModel):
    phone: str


class CodeIn(BaseModel):
    code: Optional[str] = None
    password: Optional[str] = None


class DownloadIn(BaseModel):
    link: str
    folder: str = ""
    album: bool = True


class SetupIn(BaseModel):
    api_id: str
    api_hash: str


class FolderIn(BaseModel):
    path: str = ""
    name: str


@app.on_event("startup")
async def startup():
    load_history()
    if api_creds():
        try:
            await get_client()
        except Exception:
            pass


@app.get("/", dependencies=[Depends(check_auth)])
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
async def touch_icon():
    return FileResponse(STATIC / "icons" / "apple-touch-icon.png")


@app.get("/api/me", dependencies=[Depends(check_auth)])
async def me():
    if not api_creds():
        return {"authorized": False, "step": "api"}
    c = await get_client()
    if not await c.is_user_authorized():
        return {"authorized": False, "step": login_state.get("step", "phone")}
    u = await c.get_me()
    name = " ".join(x for x in [u.first_name, u.last_name] if x)
    return {"authorized": True, "name": name, "username": u.username, "phone": u.phone}


@app.post("/api/setup", dependencies=[Depends(check_auth)])
async def setup(body: SetupIn):
    global client
    if client is not None and client.is_connected() and await client.is_user_authorized():
        raise HTTPException(400, "כבר מחובר. קודם להתנתק")
    api_id, api_hash = body.api_id.strip(), body.api_hash.strip()
    if not api_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise HTTPException(400, "api_id הוא מספר, ו־api_hash הוא 32 תווים (0-9, a-f)")
    CONFIG_FILE.write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}))
    os.chmod(CONFIG_FILE, 0o600)
    if client is not None:
        await client.disconnect()
        client = None
    return {"step": "phone"}


@app.post("/api/login/phone", dependencies=[Depends(check_auth)])
async def login_phone(body: PhoneIn):
    c = await get_client()
    phone = re.sub(r"[^\d+]", "", body.phone)
    try:
        sent = await c.send_code_request(phone)
    except errors.FloodWaitError as e:
        raise HTTPException(429, f"טלגרם מבקש לחכות {e.seconds} שניות")
    except errors.PhoneNumberInvalidError:
        raise HTTPException(400, "מספר טלפון לא תקין")
    login_state.update(step="code", phone=phone, hash=sent.phone_code_hash)
    return {"step": "code"}


@app.post("/api/login/code", dependencies=[Depends(check_auth)])
async def login_code(body: CodeIn):
    c = await get_client()
    try:
        if body.password:
            await c.sign_in(password=body.password)
        else:
            await c.sign_in(login_state.get("phone"), (body.code or "").strip(),
                            phone_code_hash=login_state.get("hash"))
    except errors.SessionPasswordNeededError:
        login_state["step"] = "password"
        return {"step": "password"}
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
        raise HTTPException(400, "הקוד שגוי או שפג תוקפו")
    except errors.PasswordHashInvalidError:
        raise HTTPException(400, "סיסמה שגויה")
    login_state.clear()
    return {"step": "done"}


@app.post("/api/logout", dependencies=[Depends(check_auth)])
async def logout():
    c = await get_client()
    await c.log_out()
    global client
    client = None
    login_state.clear()
    return {"ok": True}


@app.get("/api/folders", dependencies=[Depends(check_auth)])
async def folders(path: str = ""):
    p = safe_dir(path)
    if not p.is_dir():
        raise HTTPException(404, "התיקייה לא קיימת")
    dirs = []
    try:
        entries = list(p.iterdir())
    except PermissionError:
        raise HTTPException(403, "אין הרשאה לתיקייה הזו")
    for d in entries:
        if d.name.startswith((".", "@", "#")):
            continue
        try:
            if d.is_dir():
                dirs.append(d.name)
        except OSError:
            continue
    dirs.sort(key=str.lower)
    return {"path": rel_of(p), "root": DOWNLOAD_ROOT.name or "/", "dirs": dirs}


@app.post("/api/folders", dependencies=[Depends(check_auth)])
async def make_folder(body: FolderIn):
    name = clean_name(body.name)
    p = safe_dir(os.path.join(body.path, name))
    p.mkdir(parents=True, exist_ok=True)
    return {"path": rel_of(p)}


CHAT_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(c/)?([A-Za-z0-9_]+)/?(?:\?.*)?$"
)
ANY_MSG_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(?:c/)?[A-Za-z0-9_]+/\d+(?:/\d+)?"
)
thumb_cache: Dict[str, bytes] = {}


def parse_chat_link(link: str):
    """t.me/name or t.me/c/123 (no message id) -> (chat, label). None if it is not a chat link."""
    m = CHAT_RE.match(link.strip())
    if not m or m.group(2).lower() in ("joinchat", "addstickers", "share", "proxy"):
        return None
    private, chat = m.groups()
    return (PeerChannel(int(chat)), f"c{chat}") if private else (chat, chat)


def msg_link(label: str, msg_id: int) -> str:
    return f"https://t.me/c/{label[1:]}/{msg_id}" if re.fullmatch(r"c\d+", label) else f"https://t.me/{label}/{msg_id}"


async def open_chat(c: TelegramClient, chat):
    try:
        return await resolve_chat(c, chat)
    except (errors.ChannelPrivateError, errors.ChannelInvalidError):
        raise HTTPException(403, "אין גישה לערוץ. צריך להיות חבר בו בחשבון הזה")
    except (ValueError, TypeError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        raise HTTPException(404, "הערוץ לא נמצא (בערוץ פרטי צריך להיות חבר בו)")


def item_of(m, label: str, fallback: str) -> dict:
    return {
        "link": msg_link(label, m.id), "id": m.id, "name": media_name(m, label, fallback),
        "size": m.file.size or 0, "kind": kind_of(m), "duration": getattr(m.file, "duration", None),
        "date": m.date.timestamp() if m.date else None,
        "caption": (m.message or "").strip().splitlines()[0][:140] if (m.message or "").strip() else "",
    }


async def enqueue(m, label: str, folder: Path, link: str) -> str:
    target = unique_path(folder, media_name(m, label, folder.name if folder != DOWNLOAD_ROOT else ""))
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id, "link": link, "name": target.name,
        "folder": rel_of(folder), "kind": kind_of(m), "size": m.file.size or 0,
        "done": 0, "speed": 0, "status": "queued", "error": None,
        "created": time.time(), "thumb": await thumb_data(m),
        "duration": getattr(m.file, "duration", None),
        "caption": (m.message or "")[:500], "msg_id": m.id,
    }
    tasks[job_id] = asyncio.create_task(run_job(job_id, m, target))
    return job_id


def target_folder(rel: str) -> Path:
    folder = safe_dir(rel)
    if not folder.is_dir():
        raise HTTPException(400, "תיקיית היעד לא קיימת")
    return folder


@app.post("/api/download", dependencies=[Depends(check_auth)])
async def download(body: DownloadIn):
    c = await require_login()
    if parse_chat_link(body.link):
        return {"browse": True}  # a channel/group link: the UI opens the picker instead
    try:
        chat, msg_id, label = parse_link(body.link)
    except ValueError as e:
        raise HTTPException(400, str(e))
    folder = target_folder(body.folder)

    entity = await open_chat(c, chat)
    msg = await c.get_messages(entity, ids=msg_id)
    if not msg:
        raise HTTPException(404, "ההודעה לא נמצאה (אולי נמחקה)")

    msgs = [msg]
    if body.album and msg.grouped_id:
        around = await c.get_messages(entity, ids=list(range(msg_id - 10, msg_id + 11)))
        msgs = [m for m in around if m and m.grouped_id == msg.grouped_id]
    msgs = [m for m in msgs if m.media and m.file]
    if not msgs:
        # A post that lists other posts ("index" message) -> let the user pick from them
        if len(ANY_MSG_LINK_RE.findall(msg.message or "")) >= 1 or any(
                getattr(e, "url", None) for e in (msg.entities or [])):
            return {"browse": True}
        raise HTTPException(400, "אין בהודעה הזו קובץ להורדה")

    created = [await enqueue(m, label, folder, body.link.strip() if len(msgs) == 1 else msg_link(label, m.id))
               for m in msgs]
    save_history()
    return {"jobs": created}


class BrowseIn(BaseModel):
    link: str
    offset_id: int = 0
    limit: int = 60


@app.post("/api/browse", dependencies=[Depends(check_auth)])
async def browse(body: BrowseIn):
    """List downloadable files: every media message of a channel/group, or the posts an index message links to."""
    c = await require_login()
    limit = max(1, min(body.limit, 200))
    chat_link = parse_chat_link(body.link)
    if chat_link:
        chat, label = chat_link
        entity = await open_chat(c, chat)
        full = await c.get_entity(entity)
        title = getattr(full, "title", None) or getattr(full, "first_name", "") or label
        items, last_id, scanned = [], body.offset_id, 0
        async for m in c.iter_messages(entity, offset_id=body.offset_id, limit=limit * 8):
            scanned += 1
            last_id = m.id
            if m.media and m.file and not m.web_preview:
                items.append(item_of(m, label, title))
                if len(items) >= limit:
                    break
        more = bool(items) and len(items) >= limit or (scanned >= limit * 8)
        return {"title": title, "kind": "chat", "items": items, "next": last_id if more else None}

    # an index post: collect the message links inside it (text + hidden link entities)
    try:
        chat, msg_id, label = parse_link(body.link)
    except ValueError as e:
        raise HTTPException(400, str(e))
    entity = await open_chat(c, chat)
    msg = await c.get_messages(entity, ids=msg_id)
    if not msg:
        raise HTTPException(404, "ההודעה לא נמצאה")
    urls = ANY_MSG_LINK_RE.findall(msg.message or "")
    urls += [e.url for e in (msg.entities or []) if getattr(e, "url", None) and ANY_MSG_LINK_RE.search(e.url)]
    seen, pairs = set(), []
    for u in urls:
        try:
            ch, mid, lb = parse_link(u)
        except ValueError:
            continue
        if (lb, mid) not in seen:
            seen.add((lb, mid))
            pairs.append((ch, mid, lb))
    pairs = pairs[:300]
    groups: Dict[str, list] = {}
    for ch, mid, lb in pairs:
        groups.setdefault(lb, [ch, []])[1].append(mid)
    found: Dict[tuple, dict] = {}
    for lb, (ch, mids) in groups.items():
        try:
            ent = await resolve_chat(c, ch)
            ms = await c.get_messages(ent, ids=mids)
        except Exception:
            continue
        for m in ms:
            if m and m.media and m.file and not m.web_preview:
                found[(lb, m.id)] = item_of(m, lb, "")
    items = [found[(lb, mid)] for _, mid, lb in pairs if (lb, mid) in found]
    title = (msg.message or "").strip().splitlines()[0][:80] if (msg.message or "").strip() else "רשימת קבצים"
    return {"title": title, "kind": "index", "items": items, "next": None}


class ManyIn(BaseModel):
    links: List[str]
    folder: str = ""


@app.post("/api/download-many", dependencies=[Depends(check_auth)])
async def download_many(body: ManyIn):
    c = await require_login()
    folder = target_folder(body.folder)
    groups: Dict[str, list] = {}
    for link in body.links[:500]:
        try:
            ch, mid, lb = parse_link(link)
        except ValueError:
            continue
        groups.setdefault(lb, [ch, []])[1].append(mid)
    created = []
    for lb, (ch, mids) in groups.items():
        ent = await open_chat(c, ch)
        for i in range(0, len(mids), 100):
            for m in await c.get_messages(ent, ids=mids[i:i + 100]):
                if m and m.media and m.file:
                    created.append(await enqueue(m, lb, folder, msg_link(lb, m.id)))
    save_history()
    return {"jobs": created}


@app.get("/api/thumb", dependencies=[Depends(check_auth)])
async def thumb(link: str):
    from fastapi.responses import Response
    if link in thumb_cache:
        return Response(thumb_cache[link], media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    c = await require_login()
    try:
        ch, mid, _ = parse_link(link)
        m = await c.get_messages(await resolve_chat(c, ch), ids=mid)
        data = await c.download_media(m, thumb=-1, file=bytes) if m else None
    except Exception:
        data = None
    if not data:
        raise HTTPException(404, "no thumb")
    if len(thumb_cache) > 600:
        thumb_cache.pop(next(iter(thumb_cache)))
    thumb_cache[link] = data
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/jobs", dependencies=[Depends(check_auth)])
async def list_jobs():
    return sorted(jobs.values(), key=lambda j: j["created"])


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(check_auth)])
async def cancel_job(job_id: str):
    job = jobs.get(job_id) or {}
    t = tasks.get(job_id)
    if t:
        job["status"] = "cancelled"
        t.cancel()
    return {"ok": True}


@app.post("/api/fix-names", dependencies=[Depends(check_auth)])
async def fix_names():
    """Rename finished downloads whose name is random, using the message caption from Telegram."""
    c = await require_login()
    renamed = []
    for job in list(jobs.values()):
        if job.get("status") != "done":
            continue
        folder = safe_dir(job["folder"])
        cur = folder / job["name"]
        if not cur.exists():
            continue
        try:
            chat, msg_id, label = parse_link(job["link"])
            msg = await c.get_messages(await resolve_chat(c, chat), ids=job.get("msg_id") or msg_id)
        except Exception:
            continue
        if not msg or not msg.file:
            continue
        want = media_name(msg, label, folder.name if folder != DOWNLOAD_ROOT else "")
        if want == cur.name:
            continue
        target = unique_path(folder, want)
        cur.rename(target)
        renamed.append({"from": cur.name, "to": target.name})
        job.update(name=target.name, caption=(msg.message or "")[:500], msg_id=msg.id)
    save_history()
    return {"renamed": renamed}


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(check_auth)])
async def delete_job(job_id: str):
    if job_id in tasks:
        raise HTTPException(400, "קודם לבטל את ההורדה")
    jobs.pop(job_id, None)
    save_history()
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
