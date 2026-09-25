import os
import httpx
import json
import sys
import asyncio
import re
import glob
import time
import shutil
import random
import hashlib
import urllib.parse
import threading
import socket
from contextlib import contextmanager
import requests
import logging
from typing import Optional, Dict, Any, List
from PIL import Image
import uvicorn
from fastapi import FastAPI, HTTPException, Request
import yt_dlp
from yt_dlp.extractor.instagram import _id_to_pk
from app.db.url_utils import normalize_url

# Telegram API bağlantılarında Windows IPv6 TLS sıfırlama hatasını önlemek için IPv4 zorlama
_orig_getaddrinfo = socket.getaddrinfo
def _forced_ipv4_getaddrinfo(*args, **kwargs):
    responses = _orig_getaddrinfo(*args, **kwargs)
    ipv4_responses = [r for r in responses if r[0] == socket.AF_INET]
    return ipv4_responses or responses

socket.getaddrinfo = _forced_ipv4_getaddrinfo

# Windows asyncio soket uyumluluğu
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from dotenv import load_dotenv
import chromadb
from google import genai
import trafilatura
from trafilatura.settings import use_config as trafilatura_use_config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# 1. Supabase Repository Katmanı
from app.db.repositories.users import UserRepository
from app.db.repositories.channels import ChannelRepository
from app.db.repositories.contents import ContentRepository
from app.db.repositories.user_contents import UserContentRepository
from app.db.repositories.jobs import JobRepository

from app.db.url_utils import normalize_url

from app.services.media_utils import (
    detect_media_composition,
    optimize_image_for_analysis,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 2. Yapılandırma
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8000))

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Lutfen .env dosyasinda TELEGRAM_BOT_TOKEN ve GEMINI_API_KEY tanimlayin.")

client = genai.Client(api_key=GEMINI_API_KEY)

# Webshare Proxy Bilgileri (.env zorunlu - hardcoded fallback içermez)
RAW_PROXY_USER = os.getenv("PROXY_USER")
RAW_PROXY_PASS = os.getenv("PROXY_PASS")

PROXY_USER = urllib.parse.quote(RAW_PROXY_USER) if RAW_PROXY_USER else ""
PROXY_PASS = urllib.parse.quote(RAW_PROXY_PASS) if RAW_PROXY_PASS else ""

PROXY_IPS = [
    "31.59.20.176:6754",
    "45.38.107.97:6014",
    "198.105.121.200:6462",
    "64.137.96.74:6641",
    "198.23.243.226:6361",
    "38.154.185.97:6370",
    "84.247.60.125:6095",
    "191.96.254.138:6185",
    "31.58.9.4:6077"
]

# Genel web fetch'inde (trafilatura + requests) kullanılan tarayıcı taklidi
# header seti - varsayılan "trafilatura/x.y.z" User-Agent'i birçok sitenin
# WAF/bot-tespiti tarafından doğrudan engelleniyor.
GENERIC_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
GENERIC_WEB_HEADERS = {
    "User-Agent": GENERIC_WEB_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# trafilatura.fetch_url() yalnızca User-Agent/Cookie override'ına izin veriyor
# (Accept/Accept-Language için config desteği yok) - en azından User-Agent'i
# yukarıdakiyle aynı tutuyoruz.
_GENERIC_WEB_TRAFILATURA_CONFIG = trafilatura_use_config()
_GENERIC_WEB_TRAFILATURA_CONFIG.set("DEFAULT", "USER_AGENTS", GENERIC_WEB_USER_AGENT)

# download_media() bazı platformlarda (TikTok, generic yt-dlp fallback) yt-dlp'yi
# ignoreerrors=True ile çalıştırıyor; indirme sessizce başarısız olursa media_files
# boş, caption da boş/anlamsız kalabilir. Bu durumda Gemini'ye hiç istek atılmamalı -
# aksi halde boş girdiyle bile inandırıcı ama tamamen alakasız bir analiz üretebiliyor
# (bkz. BLUEPRINT.md §6, 11 Eylül 2026 TikTok/"Almanya hidrojen altyapısı" vakası).
MIN_CAPTION_LENGTH_FOR_ANALYSIS = 10

def get_random_proxy() -> str:
    if not PROXY_USER or not PROXY_PASS:
        raise ValueError(
            "Proxy kimlik bilgileri eksik. "
            "Lütfen .env içinde PROXY_USER ve PROXY_PASS tanımlayın."
        )

    ip_port = random.choice(PROXY_IPS)
    return f"http://{PROXY_USER}:{PROXY_PASS}@{ip_port}"


def get_proxy_requests_kwargs(timeout: int = 15) -> dict:
    proxy = get_random_proxy()
    return {
        "proxies": {
            "http": proxy,
            "https": proxy,
        },
        "timeout": timeout,
    }

# Eşzamanlı isteklerde mükerrer indirme ve Gemini çağrılarını önleyen, referans sayaçlı kilit havuzu
_url_locks_guard = threading.Lock()
_url_locks: Dict[str, list] = {}

@contextmanager
def get_url_lock(canonical_url: str):
    """Aynı kanonik URL için aynı anda tek thread çalışmasını sağlar; iş bittiğinde kilidi bellekten temizler."""
    with _url_locks_guard:
        if canonical_url not in _url_locks:
            _url_locks[canonical_url] = [threading.Lock(), 0]
        entry = _url_locks[canonical_url]
        entry[1] += 1

    lock = entry[0]
    with lock:
        try:
            yield
        finally:
            with _url_locks_guard:
                entry[1] -= 1
                if entry[1] <= 0:
                    _url_locks.pop(canonical_url, None)


def get_deterministic_doc_id(user_id: str, canonical_url: str) -> str:
    clean_url = normalize_url(canonical_url)
    h = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:24]
    return f"doc_{user_id}_{h}"

# 3. Vector DB (Chroma)
chroma_client = chromadb.PersistentClient(path="./chroma_data")
collection = chroma_client.get_or_create_collection(name="saved_instagram_posts")

awaiting_custom_lang: Dict[int, bool] = {}

def track_and_get_user(telegram_user_id: int, username: Optional[str], first_name: Optional[str], tg_lang_code: Optional[str] = None) -> tuple[str, str]:
    ext_id = str(telegram_user_id)
    channel = ChannelRepository.get_channel_by_external_id("telegram", ext_id)
    
    if channel:
        user_id = channel["user_id"]
        user = UserRepository.get_user(user_id)
        preferred_lang = user["preferred_language"] if user else "English"
        UserRepository.update_last_active(user_id)
        ChannelRepository.update_last_seen(channel["id"])
    else:
        preferred_lang = "English"
        if tg_lang_code:
            code = tg_lang_code.lower()
            if code.startswith("tr"): preferred_lang = "Türkçe"
            elif code.startswith("de"): preferred_lang = "Deutsch"
            elif code.startswith("en"): preferred_lang = "English"

        user = UserRepository.create_user(preferred_language=preferred_lang)
        user_id = user["id"]
        ChannelRepository.upsert_channel(
            user_id=user_id,
            channel_type="telegram",
            external_user_id=ext_id,
            display_name=first_name,
            username=username
        )

    return user_id, preferred_lang

def track_and_get_whatsapp_user(
    whatsapp_user_id: str,
    first_name: Optional[str] = None
) -> tuple[str, str]:

    ext_id = str(whatsapp_user_id)

    channel = ChannelRepository.get_channel_by_external_id(
        "whatsapp",
        ext_id
    )

    if channel:
        user_id = channel["user_id"]

        user = UserRepository.get_user(user_id)

        preferred_lang = (
            user["preferred_language"]
            if user
            else "English"
        )

        UserRepository.update_last_active(user_id)
        ChannelRepository.update_last_seen(channel["id"])

    else:
        # WhatsApp yeni kullanıcıları varsayılan olarak English başlatıyoruz
        preferred_lang = "English"

        user = UserRepository.create_user(
            preferred_language=preferred_lang
        )

        user_id = user["id"]

        ChannelRepository.upsert_channel(
            user_id=user_id,
            channel_type="whatsapp",
            external_user_id=ext_id,
            display_name=first_name,
            username=""
        )

    return user_id, preferred_lang

def set_user_language(telegram_user_id: int, new_language: str, username: Optional[str] = None, first_name: Optional[str] = None) -> str:
    user_id, _ = track_and_get_user(telegram_user_id, username, first_name)
    UserRepository.upsert_user(user_id=user_id, preferred_language=new_language)
    return user_id

def build_language_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_English"),
            InlineKeyboardButton("🇹🇷 Türkçe", callback_data="lang_Türkçe"),
            InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_Deutsch"),
        ],
        [
            InlineKeyboardButton("✍️ Other / Diğer (Type Any)", callback_data="lang_custom")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

web_app = FastAPI()

@web_app.api_route("/", methods=["GET", "HEAD"])
def read_root():
    return {"status": "Second Brain Bot 7/24 Aktif!"}


@web_app.get("/webhook/whatsapp")
async def whatsapp_webhook_verify(
    hub_mode: Optional[str] = None,
    hub_verify_token: Optional[str] = None,
    hub_challenge: Optional[str] = None,
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token == os.getenv("WHATSAPP_VERIFY_TOKEN")
    ):
        return int(hub_challenge)

    raise HTTPException(status_code=403, detail="Verification failed")

from fastapi import Request

async def process_whatsapp_message(
    sender: str,
    user_text: str,
    first_name: Optional[str] = None
):
    """
    Process WhatsApp messages using the existing
    Second Brain memory/RAG pipeline.
    """

    user_id, lang = track_and_get_whatsapp_user(
        whatsapp_user_id=sender,
        first_name=first_name
    )

    clean_text = user_text.lower().replace("/", "")

    # Language command
    if clean_text in ["language", "dil", "lang"]:
        return f"🌐 Current language: {lang}"

    # URL → ingest into Second Brain
    if is_generic_url(user_text):
        try:
            result = await asyncio.to_thread(
                process_url_content,
                user_text,
                user_id,
                lang
            )

            return (
                f"✅ Saved & Indexed ({lang})\n\n"
                f"🔗 {result['url']}\n\n"
                f"{result['analysis']}"
            )

        except Exception as e:
            logger.exception(
                f"[WhatsApp URL Error] {e}"
            )
            return f"❌ Error: {str(e)}"

    # Normal question → search user's memory
    results = collection.query(
        query_texts=[user_text],
        n_results=3,
        where={"user_id": user_id}
    )

    if (
        not results["documents"]
        or not results["documents"][0]
    ):
        return (
            f"🔍 No relevant content found in memory "
            f"for '{user_text}'."
        )

    retrieved_list = results["metadatas"][0]

    try:
        rag_answer = await asyncio.to_thread(
            generate_conversational_rag_response,
            user_text,
            retrieved_list,
            lang
        )

        return rag_answer

    except Exception as e:
        logger.exception(
            f"[WhatsApp RAG Error] {e}"
        )

        return (
            "🔎 Sources:\n\n"
            + "\n\n".join(
                [
                    f"🔗 {m['url']}\n"
                    f"{m.get('analysis', '')}"
                    for m in retrieved_list
                ]
            )
        )
    
async def send_whatsapp_message(to: str, text: str):
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

    if not access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is missing")

    if not phone_number_id:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is missing")

    url = f"https://graph.facebook.com/v26.0/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text,
        },
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            headers=headers,
            json=payload,
        )

    if response.status_code >= 400:
        logger.error(
            f"[WhatsApp Send Error] "
            f"Status={response.status_code} "
            f"Response={response.text}"
        )
        raise RuntimeError(
            f"WhatsApp API error: {response.text}"
        )

    logger.info(
        f"[WhatsApp] Message sent successfully to {to}"
    )

    return response.json()

@web_app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    data = await request.json()

    logger.info(f"[WhatsApp Webhook] Received: {data}")

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):

                value = change.get("value", {})
                messages = value.get("messages", [])

                # Mesaj içermeyen status eventlerini geç
                if not messages:
                    continue

                # WhatsApp kullanıcısının adını al
                contacts = value.get("contacts", [])
                first_name = None

                if contacts:
                    first_name = (
                        contacts[0]
                        .get("profile", {})
                        .get("name")
                    )

                for message in messages:

                    # Şimdilik sadece text mesajlarını işle
                    if message.get("type") != "text":
                        continue

                    sender = message.get("from")
                    text = (
                        message
                        .get("text", {})
                        .get("body", "")
                        .strip()
                    )

                    logger.info(
                        f"[WhatsApp] Message from {sender}: {text}"
                    )

                    # Second Brain AI pipeline
                    reply_text = await process_whatsapp_message(
                        sender=sender,
                        user_text=text,
                        first_name=first_name
                    )

                    # WhatsApp'a cevap gönder
                    await send_whatsapp_message(
                        to=sender,
                        text=reply_text
                    )

        return {"status": "received"}

    except Exception as e:
        logger.exception(
            f"[WhatsApp Webhook Error] {e}"
        )

        return {"status": "error"}

async def safe_reply(update: Update, text: str, reply_markup=None, max_retries: int = 3):
    for i in range(max_retries):
        try: return await update.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            if i == max_retries - 1: return None
            await asyncio.sleep(1.5)

async def safe_edit_or_send(bot, chat_id: int, message_id: Optional[int], text: str):
    if message_id:
        try: return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except: pass
    try: return await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e: logger.error(f"[TG Error]: {e}")

def generate_content_with_retry(contents: list, model: str = "gemini-3.6-flash", max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(model=model, contents=contents).text.strip()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(3 + (attempt * 2))
                continue
            raise e

def parse_source_language_and_analysis(raw_text: str) -> tuple[str, str]:
    """Çıktıdan kaynak dili ve temiz analiz gövdesini ayrıştırır. Varsayılan 'unknown' döner."""
    match = re.search(r"^Source Language:\s*([^\n\r]+)", raw_text, re.MULTILINE | re.IGNORECASE)
    if match:
        source_lang = match.group(1).strip()
        clean_text = re.sub(r"^Source Language:\s*[^\n\r]+\n*", "", raw_text, flags=re.IGNORECASE).strip()
        return source_lang, clean_text
    return "unknown", raw_text.strip()

def translate_analysis_if_needed(content_id: str, canonical_text: str, source_lang: str, target_lang: str) -> str:
    if source_lang.strip().lower() == target_lang.strip().lower() or source_lang == "unknown":
        return canonical_text
    
    cached = ContentRepository.get_translation(content_id, target_lang)
    if cached and cached.get("translated_text"):
        return cached["translated_text"]

    prompt = f"Translate the following structured AI analysis accurately into {target_lang}. Keep the exact structure:\n\n{canonical_text}"
    translated = generate_content_with_retry([prompt])
    ContentRepository.save_translation(content_id=content_id, language=target_lang, translated_text=translated)
    return translated

def is_media_url(url: str) -> bool: return bool(re.search(r"(instagram\.com|youtube\.com|youtu\.be|tiktok\.com|twitter\.com|x\.com)", url))
def is_generic_url(url: str) -> bool: return bool(re.search(r"https?://[^\s]+", url))

# 4. Medya İndirici Motoru
class InstagramGatedContentError(ValueError):
    """İçerik özel bir hesaba ait olduğunda veya giriş gerektirdiğinde fırlatılır.

    Bu durumda eski fallback zincirinin de aynı sebeple başarısız olması
    beklendiğinden, extract_instagram_post() bu hatayı yakalayıp fallback'e
    düşmez; doğrudan çağırana iletir.
    """


def _request_with_retry(request_fn, max_attempts: int = 3, backoff_seconds=(1, 2)):
    """
    request_fn: no-arg callable that performs one HTTP request and calls
    response.raise_for_status() itself, returning the response on success.

    Retries only on 429 (rate limit) or 5xx (transient server error),
    waiting backoff_seconds[i] between attempts. Any other exception
    (network error, 4xx other than 429, etc.) propagates immediately
    without retrying.
    """
    last_error = None

    for attempt in range(max_attempts):
        try:
            return request_fn()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None

            if status != 429 and not (status is not None and 500 <= status < 600):
                raise

            last_error = e

            if attempt < max_attempts - 1:
                wait = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
                logger.warning(
                    f"[Instagram Polaris] HTTP {status} alındı, {wait}s sonra "
                    f"tekrar denenecek (deneme {attempt + 1}/{max_attempts})."
                )
                time.sleep(wait)

    raise last_error


def _extract_instagram_post_polaris(url: str, output_dir: str) -> Dict[str, Any]:
    """
    Extract Instagram posts/carousels using Instagram's
    Polaris GraphQL endpoint.
    """

    os.makedirs(output_dir, exist_ok=True)

    match = re.search(
        r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)",
        url,
    )

    if not match:
        raise ValueError("Geçersiz Instagram link.")

    shortcode = match.group(1)

    post_url = f"https://www.instagram.com/p/{shortcode}/"

    session = requests.Session()

    # ---------------------------------------------------------
    # 1. Get Instagram page and LSD token
    # ---------------------------------------------------------

    proxy_kwargs = get_proxy_requests_kwargs(timeout=30)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _fetch_page():
        r = session.get(
            post_url,
            headers=headers,
            **proxy_kwargs,
        )
        r.raise_for_status()
        return r

    response = _request_with_retry(_fetch_page)

    html = response.text

    lsd_position = html.find("LSD")

    if lsd_position < 0:
        raise ValueError(
            "Instagram LSD token bulunamadı."
        )

    lsd_match = re.search(
        r'token":"([^"]+)',
        html[lsd_position:lsd_position + 500],
    )

    if not lsd_match:
        raise ValueError(
            "Instagram LSD token parse edilemedi."
        )

    lsd_token = lsd_match.group(1)

    # ---------------------------------------------------------
    # 2. Convert shortcode to media ID
    # ---------------------------------------------------------

    media_id = str(_id_to_pk(shortcode))

    logger.info(
        f"--> [Instagram Polaris] "
        f"shortcode={shortcode} "
        f"media_id={media_id}"
    )

    # ---------------------------------------------------------
    # 3. GraphQL request
    # ---------------------------------------------------------

    graphql_headers = {
        "User-Agent": "Mozilla/5.0",
        "X-FB-LSD": lsd_token,
        "X-CSRFToken": session.cookies.get("csrftoken", ""),
        "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": post_url,
    }

    graphql_data = {
        "lsd": lsd_token,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": (
            "PolarisLoggedOutDesktopWWWPostRootContentQuery"
        ),
        "server_timestamps": "true",
        "variables": json.dumps(
            {
                "media_id": media_id,
            },
            separators=(",", ":"),
        ),
        "doc_id": "27130156389949648",
    }

    def _fetch_graphql():
        r = session.post(
            "https://www.instagram.com/api/graphql",
            headers=graphql_headers,
            data=graphql_data,
            **proxy_kwargs,
        )
        r.raise_for_status()
        return r

    graphql_response = _request_with_retry(_fetch_graphql)

    try:
        data = graphql_response.json()
    except Exception as e:
        raise ValueError(
            f"Instagram GraphQL JSON parse edilemedi: {e}"
        )

    # ---------------------------------------------------------
    # 4. Extract media object
    # ---------------------------------------------------------

    try:
        media = data["data"]["xig_polaris_media"]
        logged = media["if_not_gated_logged_out"]
    except Exception as e:
        raise ValueError(
            f"Instagram medya verisi bulunamadı: {e}"
        )

    if not isinstance(logged, dict):
        raise InstagramGatedContentError(
            "Instagram içeriğine erişilemedi: içerik özel bir hesaba ait "
            "olabilir veya giriş yapılması gerekiyor."
        )

    # ---------------------------------------------------------
    # 5. Caption
    # ---------------------------------------------------------

    caption = ""

    caption_data = logged.get("caption")

    if isinstance(caption_data, dict):
        caption = caption_data.get("text", "") or ""

    # ---------------------------------------------------------
    # 6. Get carousel
    # ---------------------------------------------------------

    carousel = logged.get("carousel_media")

    if not isinstance(carousel, list) or not carousel:
        carousel = [logged]

    logger.info(
        f"--> [Instagram Polaris] "
        f"{len(carousel)} medya bulundu."
    )

    # ---------------------------------------------------------
    # 7. Download media
    # ---------------------------------------------------------

    downloaded_count = 0

    for idx, item in enumerate(carousel):

        media_type = item.get("media_type")
        media_url = None

        # Image
        if media_type == 1:

            media_url = item.get("display_uri")

            if not media_url:
                image_versions = item.get(
                    "image_versions2"
                )

                if isinstance(image_versions, dict):

                    candidates = image_versions.get(
                        "candidates"
                    )

                    if candidates:
                        media_url = candidates[0].get(
                            "url"
                        )

        # Video
        elif media_type == 2:

            video_versions = item.get(
                "video_versions"
            )

            if isinstance(video_versions, list):

                for video in video_versions:
                    if video.get("url"):
                        media_url = video["url"]
                        break

            if not media_url:
                media_url = item.get("display_uri")

        # Generic fallback
        if not media_url:
            media_url = item.get("display_uri")

        if not media_url:
            logger.warning(
                f"[Instagram] URL bulunamadı "
                f"(item {idx})"
            )
            continue

        try:

            media_response = requests.get(
                media_url,
                headers={
                    "User-Agent": headers["User-Agent"],
                    "Referer": "https://www.instagram.com/",
                },
                **proxy_kwargs,
            )

            if media_response.status_code != 200:
                logger.warning(
                    f"[Instagram] Download failed "
                    f"(item {idx}): "
                    f"{media_response.status_code}"
                )
                continue

            content = media_response.content

            if len(content) < 3000:
                logger.warning(
                    f"[Instagram] Media too small "
                    f"(item {idx}): "
                    f"{len(content)} bytes"
                )
                continue

            content_type = (
                media_response.headers
                .get("Content-Type", "")
                .lower()
            )

            if media_type == 2 or "video/" in content_type:
                extension = "mp4"
            elif "png" in content_type:
                extension = "png"
            elif "webp" in content_type:
                extension = "webp"
            else:
                extension = "jpg"

            filename = os.path.join(
                output_dir,
                f"slide_{idx:02d}.{extension}",
            )

            with open(filename, "wb") as f:
                f.write(content)

            downloaded_count += 1

            logger.info(
                f"--> [Instagram] "
                f"Downloaded {idx + 1}/{len(carousel)}: "
                f"{filename} "
                f"({len(content)} bytes)"
            )

        except Exception as e:
            logger.warning(
                f"[Instagram Media {idx}] {e}"
            )

    # ---------------------------------------------------------
    # 8. Validation
    # ---------------------------------------------------------

    if downloaded_count == 0:
        raise ValueError(
            "Instagram medyası indirilemedi."
        )

    logger.info(
        f"--> [Instagram Polaris Başarılı]: "
        f"{downloaded_count}/{len(carousel)} medya indirildi."
    )

    return {
        "caption": caption,
    }


def _extract_instagram_post_legacy(url: str, output_dir: str) -> Dict[str, Any]:
    """
    Eski oEmbed -> bridge servisleri -> yt-dlp fallback zinciri.

    Polaris GraphQL yolu teknik/geçici bir sebeple (LSD token yok, GraphQL
    parse hatası, hiç medya indirilemedi, rate limit/5xx) başarısız olduğunda
    ikinci güvenlik katmanı olarak kullanılır. Gated/özel içerik hatasında
    bu fonksiyon hiç çağrılmaz (bkz. extract_instagram_post()).
    """
    caption = ""

    match = re.search(
        r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)",
        url
    )

    if not match:
        raise ValueError("Geçersiz Instagram link.")

    shortcode = match.group(1)

    os.makedirs(output_dir, exist_ok=True)

    # ---------------------------------------------------------
    # 1. oEmbed
    # ---------------------------------------------------------
    try:
        oembed_url = (
            "https://api.instagram.com/oembed/"
            f"?url=https://www.instagram.com/p/{shortcode}/"
        )

        r_oembed = requests.get(
            oembed_url,
            timeout=8,
        )

        if r_oembed.status_code == 200:
            o_data = r_oembed.json()

            caption = o_data.get("title", "") or ""

            thumb = o_data.get("thumbnail_url")

            if thumb:
                try:
                    ir = requests.get(
                        thumb,
                        timeout=10,
                    )

                    content_type = ir.headers.get(
                        "Content-Type",
                        ""
                    ).lower()

                    if (
                        ir.status_code == 200
                        and len(ir.content) > 3000
                        and content_type.startswith("image/")
                    ):
                        with open(
                            os.path.join(
                                output_dir,
                                "slide_00.jpg",
                            ),
                            "wb",
                        ) as f:
                            f.write(ir.content)

                except Exception as thumb_err:
                    logger.warning(
                        f"[oEmbed Thumbnail]: {thumb_err}"
                    )

    except Exception as e:
        logger.warning(
            f"[oEmbed]: {e}"
        )

    # ---------------------------------------------------------
    # 2. Bridge extraction
    # ---------------------------------------------------------
    bridges = [
        f"https://api.ddinstagram.com/p/{shortcode}",
        f"https://api.vxinstagram.com/p/{shortcode}",
    ]

    for bridge_url in bridges:
        try:
            response = requests.get(
                bridge_url,
                **get_proxy_requests_kwargs(timeout=15),
            )

            if response.status_code != 200:
                continue

            data = response.json()

            caption = (
                caption
                or data.get("description", "")
                or data.get("caption", "")
                or data.get("text", "")
                or ""
            )

            media_urls = []

            media_details = data.get("media_details")

            if isinstance(media_details, list):
                for media_item in media_details:
                    media_url = (
                        media_item.get("url")
                        or media_item.get("src")
                    )

                    if media_url:
                        media_urls.append(
                            media_url
                        )

            if not media_urls:
                single_media = (
                    data.get("video_url")
                    or data.get("image")
                    or data.get("thumbnail")
                )

                if single_media:
                    media_urls.append(
                        single_media
                    )

            downloaded_count = 0

            for idx, media_url in enumerate(
                media_urls[:10]
            ):
                try:
                    media_response = requests.get(
                        media_url,
                        **get_proxy_requests_kwargs(timeout=20),
                    )

                    if (
                        media_response.status_code != 200
                        or len(media_response.content) <= 3000
                    ):
                        continue

                    header_type = media_response.headers.get(
                        "Content-Type",
                        "",
                    ).lower()

                    if "video/" in header_type:
                        extension = "mp4"
                    elif "image/" in header_type:
                        extension = "jpg"
                    else:
                        media_url_lower = media_url.lower()

                        if ".mp4" in media_url_lower:
                            extension = "mp4"
                        elif any(
                            ext in media_url_lower
                            for ext in (
                                ".jpg",
                                ".jpeg",
                                ".png",
                                ".webp",
                            )
                        ):
                            extension = "jpg"
                        else:
                            continue

                    destination = os.path.join(
                        output_dir,
                        f"slide_{idx:02d}.{extension}",
                    )

                    with open(
                        destination,
                        "wb",
                    ) as f:
                        f.write(
                            media_response.content
                        )

                    downloaded_count += 1

                except Exception as media_err:
                    logger.warning(
                        f"[Bridge Media]: {media_err}"
                    )

            if downloaded_count > 0:
                logger.info(
                    f"--> [Bridge Başarılı]: "
                    f"{downloaded_count} medya indirildi."
                )

                return {
                    "caption": caption
                }

        except Exception as bridge_err:
            logger.warning(
                f"[Bridge Deneme]: {bridge_err}"
            )

    # ---------------------------------------------------------
    # 3. yt-dlp fallback
    # ---------------------------------------------------------
    for attempt in range(2):
        proxy = get_random_proxy()

        ydl_opts = {
            "outtmpl": os.path.join(
                output_dir,
                "video_media.%(ext)s",
            ),
            "format": (
                "bestvideo[height<=720]+bestaudio/"
                "best[height<=720]/best"
            ),
            "proxy": proxy,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "socket_timeout": 15,
            "retries": 1,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        }

        try:
            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True,
                )

                if info:
                    title = info.get(
                        "title",
                        "",
                    )

                    description = (
                        info.get(
                            "description",
                            "",
                        )
                        or ""
                    )

                    if not caption:
                        caption = (
                            f"{title}\n"
                            f"{description}"
                        ).strip()

                files = glob.glob(
                    os.path.join(
                        output_dir,
                        "*.*",
                    )
                )

                if files:
                    logger.info(
                        "--> [yt-dlp Başarılı]: "
                        f"{len(files)} medya dosyası."
                    )

                    return {
                        "caption": caption
                    }

        except Exception as ydl_err:
            logger.warning(
                f"[yt-dlp Yakalama {attempt + 1}/2]: "
                f"{ydl_err}"
            )

    # ---------------------------------------------------------
    # 4. Final validation
    # ---------------------------------------------------------
    media_files = glob.glob(
        os.path.join(
            output_dir,
            "*.*",
        )
    )

    if not media_files:
        raise ValueError(
            "Instagram medyası indirilemedi "
            "ve gönderiye erişilemedi."
        )

    return {
        "caption": caption
    }


def extract_instagram_post(url: str, output_dir: str) -> Dict[str, Any]:
    """
    Instagram içeriğini önce Polaris GraphQL ile çekmeyi dener.

    Polaris teknik/geçici bir sebeple başarısız olursa (LSD token yok,
    GraphQL parse hatası, hiç medya indirilemedi, rate limit/5xx) eski
    oEmbed -> bridge -> yt-dlp zincirine düşer. Gated/özel içerik hatasında
    (InstagramGatedContentError) fallback denenmez; eski zincir de aynı
    sebeple başarısız olacağından hata doğrudan çağırana iletilir.
    """
    try:
        return _extract_instagram_post_polaris(url, output_dir)
    except InstagramGatedContentError:
        raise
    except Exception as e:
        logger.warning(
            f"[Instagram Polaris] Teknik hata nedeniyle eski zincire "
            f"düşülüyor: {e}",
            exc_info=True,
        )

        for stray in glob.glob(os.path.join(output_dir, "*.*")):
            try:
                os.remove(stray)
            except OSError:
                pass

        return _extract_instagram_post_legacy(url, output_dir)


def download_media(url: str, output_dir: str) -> Dict[str, Any]:
    if "instagram.com" in url:
        return extract_instagram_post(url, output_dir)

    platform = (
        "tiktok"
        if "tiktok.com" in url
        else "youtube"
        if ("youtube.com" in url or "youtu.be" in url)
        else "generic"
    )

    os.makedirs(output_dir, exist_ok=True)

    if platform == "youtube":
        # No forced extractor_args/player_client: yt-dlp's own default
        # client selection is actively maintained against YouTube's bot
        # checks, whereas a hardcoded client list (e.g. android/ios/web)
        # goes stale as YouTube blocks individual clients over time.
        ydl_opts = {
            "outtmpl": os.path.join(
                output_dir,
                "media_%(autonumber)s.%(ext)s",
            ),
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 2,
        }

    elif platform == "tiktok":
        proxy = get_random_proxy()

        ydl_opts = {
            "outtmpl": os.path.join(
                output_dir,
                "media_%(autonumber)s.%(ext)s",
            ),
            "format": "best[height<=720]/best",
            "proxy": proxy,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "socket_timeout": 15,
            "retries": 2,
        }

    else:
        ydl_opts = {
            "outtmpl": os.path.join(
                output_dir,
                "media_%(autonumber)s.%(ext)s",
            ),
            "format": "best[height<=720]/best",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "socket_timeout": 15,
            "retries": 2,
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

        caption = ""

        if info:
            title = info.get("title", "") or ""
            description = info.get("description", "") or ""
            caption = f"{title}\n{description}".strip()

        return {
            "caption": caption,
        }

# 5. Multimodal Kanonik Analiz Motoru
class GeminiFileProcessingError(RuntimeError):
    """Yüklenen bir Gemini dosyası ACTIVE duruma geçemediğinde fırlatılır."""


def _wait_for_active_file(uploaded, poll_interval: int = 5, max_attempts: int = 12):
    """
    Bir Gemini File nesnesini ACTIVE duruma geçene kadar bekler.

    Google'ın dokümante ettiği desenle uyumlu (ACTIVE olana KADAR bekle,
    yalnızca PROCESSING olduğu SÜRECE değil), buna ek olarak:
    - FAILED durumunu ayrıca kontrol eder ve hemen açıklayıcı hata fırlatır.
    - Üst sınır uygular (varsayılan 12 deneme * 5s = 60s); süre dolarsa
      dosyanın sonsuza kadar PROCESSING'de takılı kalmasını önleyecek
      açıklayıcı bir hata fırlatır.
    """
    attempts = 0
    while uploaded.state.name != "ACTIVE":
        if uploaded.state.name == "FAILED":
            raise GeminiFileProcessingError(
                f"Gemini dosya işleme başarısız oldu: {uploaded.name} (state=FAILED)"
            )
        if attempts >= max_attempts:
            raise GeminiFileProcessingError(
                f"Gemini dosyası {max_attempts * poll_interval} saniye içinde "
                f"ACTIVE duruma geçmedi: {uploaded.name} (son durum={uploaded.state.name})"
            )
        attempts += 1
        time.sleep(poll_interval)
        uploaded = client.files.get(name=uploaded.name)
    return uploaded


def analyze_canonical_multimodal_content(caption: str, media_files: List[str]) -> tuple[str, str]:
    prompt = f"""
    You are an expert AI multimodal knowledge extractor.
    
    INSTRUCTIONS:
    1. First, detect the primary source language of the content (from video speech, image text, slides, or caption).
    2. Write the first line strictly as: "Source Language: <Language Name>" (e.g., English, Turkish, German).
    3. Output the rest of your ENTIRE analysis strictly in that detected original source language.
    
    Carefully inspect ALL provided images/video slides in sequence and the caption text below.
    Extract the EXACT factual information, lessons, steps, infographics, text written in slides, and key takeaways shown.
    
    Provided context/caption:
    {caption if caption else 'None'}
    
    Required response format:
    Source Language: [Detected source language]
    Category: [...]
    Summary: [Comprehensive detailed summary of all slides/video content in the source language]
    Tags: [4-5 relevant comma-separated tags]
    """
    contents = []
    uploaded_files = []
    try:
        valid_files = sorted(media_files)
        for path in valid_files:
            if path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                try:
                    contents.append(Image.open(path))
                except: pass
            elif path.lower().endswith(('.mp4', '.mov', '.webm')):
                uploaded = client.files.upload(file=path)
                uploaded = _wait_for_active_file(uploaded)
                uploaded_files.append(uploaded)
                contents.append(uploaded)
                
        contents.append(prompt)
        raw_output = generate_content_with_retry(contents, model="gemini-3.6-flash")
        return parse_source_language_and_analysis(raw_output)
    finally:
        for uf in uploaded_files:
            try: client.files.delete(name=uf.name)
            except: pass

def analyze_canonical_web_text(text: str, title: str = "") -> tuple[str, str]:
    prompt = f"""
    Analyze the following web article.
    
    INSTRUCTIONS:
    1. Detect the primary source language of the article.
    2. Write the first line strictly as: "Source Language: <Language Name>".
    3. Output the summary strictly in that original source language.
    
    Format:
    Source Language: [Detected language]
    Category: [...]
    Summary: [Comprehensive detailed summary in source language]
    Tags: [4-5 relevant comma-separated tags]
    
    Article Title: {title}
    Article Content:
    {text[:6000]}
    """
    raw_output = generate_content_with_retry([prompt], model="gemini-3.6-flash")
    return parse_source_language_and_analysis(raw_output)

def _fetch_generic_web_text(url: str) -> Optional[str]:
    """Genel web sayfası metnini önce proxy'li tarayıcı-taklidi istekle, olmazsa trafilatura'nın kendi fetcher'ıyla çeker."""
    extracted_text = None

    try:
        p = random.choice(PROXY_IPS)
        if not PROXY_USER or not PROXY_PASS:
            raise ValueError("Proxy credentials missing in .env")
        proxy_url = f"http://{PROXY_USER}:{PROXY_PASS}@{p}"
        resp = requests.get(
            url,
            headers=GENERIC_WEB_HEADERS,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=15,
        )
        if resp.status_code == 200:
            extracted_text = trafilatura.extract(resp.text)
    except Exception as e:
        logger.warning(f"[Generic Web] İlk deneme başarısız: {e}")

    if not extracted_text:
        downloaded = trafilatura.fetch_url(url, config=_GENERIC_WEB_TRAFILATURA_CONFIG)
        if downloaded:
            extracted_text = trafilatura.extract(downloaded)

    return extracted_text

# 6. İçerik İşleme (Kilit Korumalı, 4 Durumlu Yaşam Döngüsü & Kanonik Pipeline)
def process_url_content(url: str, user_id: str, user_lang: str) -> Dict[str, Any]:
    canonical_url = normalize_url(url)
    
    # URL bazlı process-local kilit ile mükerrer indirme ve Gemini işleri engellenir
    with get_url_lock(canonical_url):
        existing_content = ContentRepository.get_by_canonical_url(canonical_url)
        
        # CASE 2: Zaten tamamlanmış içerik -> Yeniden kullan
        if existing_content and existing_content.get("status") == "completed":
            content_id = existing_content["id"]
            latest_analysis = ContentRepository.get_latest_analysis(content_id)
            if latest_analysis:
                logger.info(f"🎯 [Supabase Content Cache Hit]: {canonical_url}")
                source_lang = latest_analysis.get("source_language") or existing_content.get("original_language") or "unknown"
                presentation_text = translate_analysis_if_needed(
                    content_id=content_id,
                    canonical_text=latest_analysis.get("raw_analysis", ""),
                    source_lang=source_lang,
                    target_lang=user_lang
                )
            else:
                presentation_text = "Content processed."
                
        # CASE 1, 3, 4: Yeni içerik VEYA Mevcut başarısız/devam eden içerik
        else:
            if existing_content:
                # CASE 3 & 4: Mevcut kaydı tekrar kullan (Yeni satır OLUŞTURMAZ)
                content_id = existing_content["id"]
                ContentRepository.update_status(content_id=content_id, status="processing")
                logger.info(f"🔄 [Content Retrying/Resuming]: {canonical_url} (ID: {content_id}, Previous Status: {existing_content.get('status')})")
            else:
                # CASE 1: İlk defa görülen içerik -> Yeni kayıt oluştur
                content = ContentRepository.create_content(
                    canonical_url=canonical_url,
                    status="processing"
                )
                content_id = content["id"]

            job = JobRepository.create_job(content_id=content_id, job_type="analysis")
            JobRepository.mark_processing(job["id"])
            detected_type = "unknown"
            try:
                if is_media_url(url):
                    temp_dir = (
                        f"temp_media_{user_id[:8]}_{int(time.time())}"
                    )
                    os.makedirs(temp_dir, exist_ok=True)

                    try:
                        media_info = download_media(
                            url,
                            temp_dir,
                        )

                        media_files = glob.glob(
                            os.path.join(temp_dir, "*.*")
                        )

                        detected_type = detect_media_composition(
                            temp_dir
                        )
                        logger.info(f"[MEDIA DEBUG] detected_type={detected_type}")
                        logger.info(f"[MEDIA DEBUG] media_files={media_files}")
                        for media_file in media_files:
                            if media_file.lower().endswith(
                                (".jpg", ".jpeg", ".png", ".webp")
                            ):
                                optimize_image_for_analysis(
                                    media_file
                                )

                        

                        caption_text = media_info.get("caption", "")[:1500]

                        if not media_files and len(caption_text.strip()) < MIN_CAPTION_LENGTH_FOR_ANALYSIS:
                            raise ValueError(
                                "İndirilen medya veya metin bulunamadı, analiz yapılamıyor."
                            )

                        source_lang, canonical_analysis = (
                            analyze_canonical_multimodal_content(
                                caption_text,
                                media_files=media_files,
                            )
                        )

                    finally:
                        if os.path.exists(temp_dir):
                            shutil.rmtree(
                                temp_dir,
                                ignore_errors=True,
                            )

                elif is_generic_url(url):
                    extracted_text = _fetch_generic_web_text(url)
                    if not extracted_text: raise ValueError("Web page could not be fetched.")
                    detected_type = "article"
                    source_lang, canonical_analysis = analyze_canonical_web_text(extracted_text)
                else:
                    raise ValueError("Invalid URL format.")

                # Kanonik analizi ve tespit edilen orijinal dili kaydet
                ContentRepository.update_content(content_id, {"content_type": detected_type, "original_language": source_lang})
                ContentRepository.save_canonical_analysis(
                    content_id=content_id,
                    summary=canonical_analysis[:200],
                    raw_analysis=canonical_analysis,
                    source_language=source_lang
                )
                ContentRepository.update_status(content_id=content_id, status="completed")
                JobRepository.mark_completed(job_id=job["id"])

                # Kullanıcı sunumu için gerekirse çevir
                presentation_text = translate_analysis_if_needed(
                    content_id=content_id,
                    canonical_text=canonical_analysis,
                    source_lang=source_lang,
                    target_lang=user_lang
                )
            except Exception as e:
                JobRepository.mark_failed(job_id=job["id"], error_message=str(e))
                ContentRepository.update_status(content_id=content_id, status="failed")
                raise e

        # User-Content ilişkisini Supabase'e kaydet
        UserContentRepository.save_content(user_id=user_id, content_id=content_id, user_language=user_lang)

        # Chroma Vector Indexing
        doc_id = get_deterministic_doc_id(user_id, canonical_url)
        collection.upsert(
            documents=[f"{presentation_text}\n{canonical_url}"],
            metadatas=[{"user_id": user_id, "url": canonical_url, "analysis": presentation_text}],
            ids=[doc_id]
        )
        return {"url": canonical_url, "analysis": presentation_text}

def generate_conversational_rag_response(user_query: str, retrieved_records: list, target_lang: str) -> str:
    context_str = "\n".join([f"Source URL: {r['url']}\nContent: {r['analysis']}\n---" for r in retrieved_records])
    prompt = f"""
    You are the user's personal Second Brain AI assistant.
    You MUST respond completely and naturally in the language: {target_lang}.
    
    Stored records from memory:
    {context_str}
    
    User Query: "{user_query}"
    
    TASK:
    1. Answer the query directly and clearly based on the stored records in {target_lang}.
    2. Format with clean bullet points.
    3. At the end, list the relevant source links.
    """
    return generate_content_with_retry([prompt], model="gemini-3.6-flash")

# 7. Telegram Handlerları
async def send_language_menu(update: Update, user_id: str, current_lang: str):
    reply_markup = build_language_keyboard()
    msg = f"🌐 **Current Language:** `{current_lang}`\n\nSelect your language:"
    await safe_reply(update, msg, reply_markup=reply_markup)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)
    welcome_text = f"🧠 **Second Brain AI ({lang})**\n\n• 🔗 **Link:** Send any Instagram, YouTube, TikTok or Web link.\n• 📸 **Direct Media:** Send photos, videos or screenshots directly to store.\n• 💬 **Ask:** Search your memory anytime via text or voice."
    await safe_reply(update, welcome_text)

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)
    await send_language_menu(update, user_id, lang)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)
    
    users = UserRepository.get_all_users()
    total_saves = UserContentRepository.count_total_saves()
    
    user_lines = [f"• `{u['id']}` [{u.get('preferred_language', 'English')}]" for u in users]
    report = f"📊 **Statistics**\n\n👥 **Users Count:** `{len(users)}`\n📁 **Total Saved Items:** `{total_saves}`\n\n🆔 **Users:**\n" + ("\n".join(user_lines) if user_lines else "None yet.")
    await safe_reply(update, report)

async def handle_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    
    if data == "lang_custom":
        awaiting_custom_lang[user.id] = True
        await query.edit_message_text("✍️ Please type and send the language you want to use:")
    elif data.startswith("lang_"):
        chosen_lang = data.replace("lang_", "")
        set_user_language(user.id, chosen_lang, user.username, user.first_name)
        awaiting_custom_lang[user.id] = False
        await query.edit_message_text(f"✅ Language set to: **{chosen_lang}**")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)

    status_msg = await safe_reply(update, f"⏳ [{lang}] Analyzing image...")
    msg_id = status_msg.message_id if status_msg else None
    temp_img_path = f"temp_photo_{user.id}_{int(time.time())}.jpg"

    # 1. İçeriği processing durumunda oluştur
    content = ContentRepository.create_content(
        source_platform="direct_upload",
        content_type="image",
        caption=update.message.caption or "",
        status="processing"
    )
    content_id = content["id"]

    try:
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        await photo_file.download_to_drive(temp_img_path)
        
        # 2. Kanonik kaynak dilinde analiz
        source_lang, canonical_analysis = await asyncio.to_thread(
            analyze_canonical_multimodal_content,
            update.message.caption or "",
            [temp_img_path]
        )
        
        # 3. Kanonik analiz ve dili kaydet, durumu completed yap
        ContentRepository.update_content(content_id, {"original_language": source_lang})
        ContentRepository.save_canonical_analysis(
            content_id=content_id,
            summary=canonical_analysis[:200],
            raw_analysis=canonical_analysis,
            source_language=source_lang
        )
        ContentRepository.update_status(content_id=content_id, status="completed")
        UserContentRepository.save_content(user_id=user_id, content_id=content_id, user_language=lang)

        # 4. Sunum için çeviri
        presentation_text = translate_analysis_if_needed(
            content_id=content_id,
            canonical_text=canonical_analysis,
            source_lang=source_lang,
            target_lang=lang
        )

        doc_id = f"photo_{user_id}_{int(time.time())}"
        collection.upsert(
            documents=[f"{presentation_text}\n[Direct Photo]"],
            metadatas=[{"user_id": user_id, "url": "[Direct Photo]", "analysis": presentation_text}],
            ids=[doc_id]
        )
        reply = f"✅ **Saved & Analyzed ({lang})**\n\n{presentation_text}"
        await safe_edit_or_send(context.bot, chat_id, msg_id, reply)
    except Exception as e:
        ContentRepository.update_status(content_id=content_id, status="failed")
        await safe_edit_or_send(context.bot, chat_id, msg_id, f"❌ Error: {e}")
    finally:
        if os.path.exists(temp_img_path): os.remove(temp_img_path)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)

    status_msg = await safe_reply(update, f"⏳ [{lang}] Analyzing video...")
    msg_id = status_msg.message_id if status_msg else None
    temp_vid_path = f"temp_video_{user.id}_{int(time.time())}.mp4"

    # 1. İçeriği processing durumunda oluştur
    content = ContentRepository.create_content(
        source_platform="direct_upload",
        content_type="video",
        caption=update.message.caption or "",
        status="processing"
    )
    content_id = content["id"]

    try:
        video = update.message.video
        video_file = await context.bot.get_file(video.file_id)
        await video_file.download_to_drive(temp_vid_path)
        
        # 2. Kanonik kaynak dilinde analiz
        source_lang, canonical_analysis = await asyncio.to_thread(
            analyze_canonical_multimodal_content,
            update.message.caption or "",
            [temp_vid_path]
        )
        
        # 3. Kanonik analiz ve dili kaydet, durumu completed yap
        ContentRepository.update_content(content_id, {"original_language": source_lang})
        ContentRepository.save_canonical_analysis(
            content_id=content_id,
            summary=canonical_analysis[:200],
            raw_analysis=canonical_analysis,
            source_language=source_lang
        )
        ContentRepository.update_status(content_id=content_id, status="completed")
        UserContentRepository.save_content(user_id=user_id, content_id=content_id, user_language=lang)

        # 4. Sunum için çeviri
        presentation_text = translate_analysis_if_needed(
            content_id=content_id,
            canonical_text=canonical_analysis,
            source_lang=source_lang,
            target_lang=lang
        )

        doc_id = f"video_{user_id}_{int(time.time())}"
        collection.upsert(
            documents=[f"{presentation_text}\n[Direct Video]"],
            metadatas=[{"user_id": user_id, "url": "[Direct Video]", "analysis": presentation_text}],
            ids=[doc_id]
        )
        reply = f"✅ **Saved & Analyzed ({lang})**\n\n{presentation_text}"
        await safe_edit_or_send(context.bot, chat_id, msg_id, reply)
    except Exception as e:
        ContentRepository.update_status(content_id=content_id, status="failed")
        await safe_edit_or_send(context.bot, chat_id, msg_id, f"❌ Error: {e}")
    finally:
        if os.path.exists(temp_vid_path): os.remove(temp_vid_path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)
    
    if awaiting_custom_lang.get(user.id, False):
        set_user_language(user.id, user_text, user.username, user.first_name)
        awaiting_custom_lang[user.id] = False
        await safe_reply(update, f"✅ Language updated to: **{user_text}**")
        return

    clean_text = user_text.lower().replace("/", "")
    if clean_text in ["language", "dil", "lang"]:
        await send_language_menu(update, user_id, lang)
        return

    if is_generic_url(user_text):
        status_msg = await safe_reply(update, f"⏳ [{lang}] Ingesting and analyzing...")
        msg_id = status_msg.message_id if status_msg else None
        try:
            result = await asyncio.to_thread(process_url_content, user_text, user_id, lang)
            reply = f"✅ **Saved & Indexed ({lang})**\n\n🔗 {result['url']}\n\n{result['analysis']}"
            await safe_edit_or_send(context.bot, chat_id, msg_id, reply)
        except Exception as e:
            await safe_edit_or_send(context.bot, chat_id, msg_id, f"❌ Error: {str(e)}")
    else:
        results = collection.query(query_texts=[user_text], n_results=3, where={"user_id": user_id})
        if not results['documents'] or not results['documents'][0]:
            await safe_reply(update, f"🔍 No relevant content found in memory for '{user_text}'.")
            return

        retrieved_list = results['metadatas'][0]
        status_msg = await safe_reply(update, f"🤔 Searching memory ({lang})...")
        msg_id = status_msg.message_id if status_msg else None
        try:
            rag_answer = await asyncio.to_thread(generate_conversational_rag_response, user_text, retrieved_list, lang)
            await safe_edit_or_send(context.bot, chat_id, msg_id, rag_answer)
        except Exception:
            fallback = f"🔎 Sources:\n\n" + "\n\n".join([f"🔗 {m['url']}\n{m.get('analysis', '')}" for m in retrieved_list])
            await safe_edit_or_send(context.bot, chat_id, msg_id, fallback)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    voice = update.message.voice
    user_id, lang = track_and_get_user(user.id, user.username, user.first_name, user.language_code)

    status_msg = await safe_reply(update, "🎙️ Transcribing voice note...")
    msg_id = status_msg.message_id if status_msg else None
    temp_voice_path = f"temp_voice_{user.id}_{int(time.time())}.ogg"
    try:
        voice_file = await context.bot.get_file(voice.file_id)
        await voice_file.download_to_drive(temp_voice_path)
        
        uploaded = client.files.upload(file=temp_voice_path)
        uploaded = await asyncio.to_thread(_wait_for_active_file, uploaded)

        transcribed_text = generate_content_with_retry([uploaded, "Transcribe audio verbatim."], model="gemini-3.6-flash").strip()
        client.files.delete(name=uploaded.name)
        
        await safe_edit_or_send(context.bot, chat_id, msg_id, f"🗣️ *'{transcribed_text}'* searching...")
        results = collection.query(query_texts=[transcribed_text], n_results=3, where={"user_id": user_id})
        if not results['documents'] or not results['documents'][0]:
            await safe_reply(update, f"🔍 Nothing found in memory for: *{transcribed_text}*")
            return
        rag_answer = await asyncio.to_thread(generate_conversational_rag_response, transcribed_text, results['metadatas'][0], lang)
        await safe_reply(update, rag_answer)
    except Exception as e:
        await safe_edit_or_send(context.bot, chat_id, msg_id, f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(temp_voice_path): os.remove(temp_voice_path)

# 8. Ana Başlatıcı
async def run_bot_and_web():
    t_request = HTTPXRequest(connection_pool_size=16, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)
    tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
    
    tg_app.add_handler(CommandHandler("start", start_command))
    tg_app.add_handler(CommandHandler(["language", "dil"], language_command))
    tg_app.add_handler(CommandHandler("stats", stats_command))
    tg_app.add_handler(CallbackQueryHandler(handle_language_callback, pattern=r"^lang_"))
    tg_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    tg_app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg_app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    logger.info("🚀 Second Brain Bot (Supabase Backend) Online!")

    server = uvicorn.Server(uvicorn.Config(web_app, host="0.0.0.0", port=PORT, log_level="warning"))
    logger.info(f"🌐 FastAPI Web Endpoint Online on Port {PORT}!")
    try: await server.serve()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

if __name__ == "__main__":
    asyncio.run(run_bot_and_web())