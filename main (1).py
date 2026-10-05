import asyncio
import json
import logging
import os
import random
import re
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.photos import UploadProfilePhotoRequest


# ============================================================
# CONFIG
# ============================================================
# متطلبات التثبيت:
#   pip install -U aiogram telethon
#
# للحصول على بحث صور آلي:
#   PIXABAY_API_KEY=xxxxxxxx
#
# إذا لم تضع مفتاح Pixabay، سيستخدم البوت Wikimedia Commons
# كمصدر بديل عام.
#
# يمكن وضع القيم كمتغيرات بيئية، أو كتابتها مباشرة هنا.

BOT_TOKEN = "8988065622:AAEvDrt2eITreBUY7btwpxVypQbu1Mp6jHk"

API_ID = 38071059
API_HASH = "114ad52cfe74e3c236dcab51eefd9963"
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+963944563559")

# اختياري. لا يحتاجه وضع الصور اليدوي.
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "57863928-956730016635a9f8a565933cf").strip()

# Pixabay أو Wikimedia.
IMAGE_SOURCE = os.getenv("IMAGE_SOURCE", "pixabay").strip().lower()

# ملف جلسة الحساب الشخصي. لا تشاركه مع أي شخص.
SESSION_NAME = os.getenv("SESSION_NAME", "my_personal_account")

# Bot API يسمح حاليًا بتنزيل ملفات حتى 20MB عبر getFile.
MAX_FILE_SIZE = 20 * 1024 * 1024

# حد أقصى لعدد الصور في أمر واحد.
MAX_AUTO_IMAGES = 500

# مهلة بين الصور لتقليل احتمالات FloodWait.
DELAY_BETWEEN_UPLOADS = float(
    os.getenv("DELAY_BETWEEN_UPLOADS", "5.0")
)

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./profile_uploads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# كلمات بحث افتراضية عندما يكتب المستخدم /auto 10 فقط.
DEFAULT_QUERIES = [
    "aesthetic nature",
    "dark aesthetic",
    "minimal aesthetic",
    "cinematic landscape",
    "mountains sunset",
    "ocean aesthetic",
    "city night aesthetic",
    "black and white portrait",
    "luxury architecture",
    "beautiful landscape",
]

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("profile-photo-bot")


# ============================================================
# GLOBALS
# ============================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

user_client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
)

upload_lock = asyncio.Lock()
OWNER_ID: int | None = None
auto_lock = asyncio.Lock()


# ============================================================
# HTTP HELPERS
# ============================================================
def http_get_json(url: str, headers: dict | None = None) -> dict:
    req = Request(
        url,
        headers=headers
        or {
            "User-Agent": (
                "Mozilla/5.0 TelegramProfilePhotoBot/1.0"
            )
        },
    )
    with urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def http_download(url: str, destination: Path) -> None:
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 TelegramProfilePhotoBot/1.0"
            )
        },
    )

    with urlopen(req, timeout=60) as response:
        content_type = (
            response.headers.get("Content-Type", "") or ""
        ).lower()

        data = response.read()

    # منع تنزيل ملفات HTML بدل الصور.
    if (
        content_type
        and not content_type.startswith("image/")
        and "octet-stream" not in content_type
    ):
        raise ValueError("المصدر لم يُرجع ملف صورة صالحًا.")

    if not data:
        raise ValueError("الصورة التي أعادها المصدر فارغة.")

    if len(data) > MAX_FILE_SIZE:
        raise ValueError("حجم الصورة أكبر من الحد المسموح.")

    destination.write_bytes(data)


async def async_get_json(url: str, headers: dict | None = None) -> dict:
    return await asyncio.to_thread(http_get_json, url, headers)


async def async_download(url: str, destination: Path) -> None:
    await asyncio.to_thread(http_download, url, destination)


# ============================================================
# CONFIG CHECK
# ============================================================
def check_config() -> None:
    errors = []

    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        errors.append("BOT_TOKEN")

    if not API_ID or API_ID == 123456:
        errors.append("API_ID")

    if not API_HASH or API_HASH == "PUT_YOUR_API_HASH_HERE":
        errors.append("API_HASH")

    if not PHONE_NUMBER or PHONE_NUMBER == "+213XXXXXXXXX":
        errors.append("PHONE_NUMBER")

    if IMAGE_SOURCE not in {"pixabay", "wikimedia"}:
        raise RuntimeError(
            "IMAGE_SOURCE يجب أن تكون pixabay أو wikimedia."
        )

    if IMAGE_SOURCE == "pixabay" and not PIXABAY_API_KEY:
        logger.warning(
            "لم يتم وضع PIXABAY_API_KEY، سيتم استخدام Wikimedia Commons."
        )


# ============================================================
# SECURITY
# ============================================================
def is_owner(message: Message) -> bool:
    return bool(
        message.from_user
        and OWNER_ID is not None
        and message.from_user.id == OWNER_ID
        and message.chat.type == "private"
    )


async def reject_non_owner(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("هذا البوت مخصص لحساب المالك فقط.")


# ============================================================
# MANUAL IMAGE DOWNLOAD
# ============================================================
async def download_telegram_file(message: Message) -> tuple[Path, str]:
    file_id = None
    original_name = "profile.jpg"

    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        original_name = f"profile_{photo.file_unique_id}.jpg"

        if photo.file_size and photo.file_size > MAX_FILE_SIZE:
            raise ValueError("حجم الصورة أكبر من 20MB.")

    elif message.document:
        document = message.document
        mime = (document.mime_type or "").lower()

        if not mime.startswith("image/"):
            raise ValueError("أرسل صورة فقط.")

        file_id = document.file_id
        original_name = (
            document.file_name
            or f"profile_{document.file_unique_id}"
        )

        if document.file_size and document.file_size > MAX_FILE_SIZE:
            raise ValueError("حجم الصورة أكبر من 20MB.")

    else:
        raise ValueError("أرسل صورة فقط.")

    suffix = Path(original_name).suffix.lower()
    if suffix not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
    }:
        suffix = ".jpg"

    fd, temp_name = tempfile.mkstemp(
        prefix="telegram_",
        suffix=suffix,
        dir=DOWNLOAD_DIR,
    )
    os.close(fd)

    target = Path(temp_name)

    try:
        await bot.download(file_id, destination=target)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    return target, original_name


# ============================================================
# IMAGE SEARCH
# ============================================================
def clean_query(query: str) -> str:
    query = re.sub(r"\s+", " ", query).strip()
    return query[:100]


async def search_pixabay(query: str, count: int) -> list[dict]:
    if not PIXABAY_API_KEY:
        return []

    params = {
        "key": PIXABAY_API_KEY,
        "q": clean_query(query),
        "image_type": "photo",
        "orientation": "all",
        "safesearch": "true",
        "per_page": min(max(count * 3, 20), 100),
        "page": 1,
    }

    url = "https://pixabay.com/api/?" + urlencode(params)
    data = await async_get_json(
        url,
        headers={"User-Agent": "TelegramProfilePhotoBot/1.0"},
    )

    results = []

    for item in data.get("hits", []):
        image_url = (
            item.get("largeImageURL")
            or item.get("webformatURL")
            or item.get("previewURL")
        )

        if not image_url:
            continue

        results.append(
            {
                "url": image_url,
                "source": "Pixabay",
                "page_url": item.get("pageURL", ""),
                "id": str(item.get("id", "")),
                "tags": item.get("tags", ""),
            }
        )

        if len(results) >= count:
            break

    return results


async def search_wikimedia(query: str, count: int) -> list[dict]:
    """
    بحث في Wikimedia Commons باستخدام MediaWiki Action API.
    نطلب namespace=6 (File) ونستخرج imageinfo بالـ URL.
    """
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": clean_query(query),
        "gsrnamespace": "6",
        "gsrlimit": min(max(count * 4, 20), 50),
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": "1280",
    }

    url = (
        "https://commons.wikimedia.org/w/api.php?"
        + urlencode(params)
    )

    data = await async_get_json(
        url,
        headers={
            "User-Agent": (
                "TelegramProfilePhotoBot/1.0 "
                "(image search bot)"
            )
        },
    )

    pages = data.get("query", {}).get("pages", {})
    results = []

    for page in pages.values():
        title = page.get("title", "")
        image_info = page.get("imageinfo") or []

        if not image_info:
            continue

        info = image_info[0]
        mime = (info.get("mime") or "").lower()

        if not mime.startswith("image/"):
            continue

        image_url = (
            info.get("thumburl")
            or info.get("url")
        )

        if not image_url:
            continue

        results.append(
            {
                "url": image_url,
                "source": "Wikimedia Commons",
                "page_url": (
                    "https://commons.wikimedia.org/wiki/"
                    + title.replace(" ", "_")
                ),
                "id": str(page.get("pageid", "")),
                "tags": title,
            }
        )

        if len(results) >= count:
            break

    return results


async def search_images(query: str, count: int) -> list[dict]:
    """
    يحاول Pixabay أولًا إذا كان API key موجودًا.
    وإذا لم يجد نتائج، يرجع إلى Wikimedia Commons.
    """
    results: list[dict] = []

    if IMAGE_SOURCE == "pixabay" and PIXABAY_API_KEY:
        try:
            results = await search_pixabay(query, count)
        except Exception as exc:
            logger.exception(
                "فشل بحث Pixabay: %s",
                exc,
            )

    if not results:
        try:
            results = await search_wikimedia(query, count)
        except Exception as exc:
            logger.exception(
                "فشل بحث Wikimedia: %s",
                exc,
            )

    # حذف التكرارات حسب الرابط.
    unique = []
    seen = set()

    for item in results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)

    return unique[:count]


# ============================================================
# PROFILE PHOTO UPLOAD
# ============================================================
async def upload_as_profile_photo(path: Path) -> None:
    async with upload_lock:
        for attempt in range(2):
            try:
                uploaded_file = await user_client.upload_file(
                    str(path)
                )

                await user_client(
                    UploadProfilePhotoRequest(
                        file=uploaded_file,
                    )
                )

                await asyncio.sleep(DELAY_BETWEEN_UPLOADS)
                return

            except FloodWaitError as exc:
                if attempt == 1:
                    raise

                wait_seconds = max(int(exc.seconds), 1)

                logger.warning(
                    "FloodWait: الانتظار %s ثانية.",
                    wait_seconds,
                )

                await asyncio.sleep(wait_seconds + 1)

            except RPCError:
                raise


# ============================================================
# SEND AUTO STATUS
# ============================================================
async def process_auto_images(
    message: Message,
    count: int,
    query: str,
) -> None:
    async with auto_lock:
        status = await message.answer(
            "🔎 جاري البحث عن الصور..."
        )

        try:
            images = await search_images(query, count)

            if not images:
                await status.edit_text(
                    "❌ لم أجد صورًا مناسبة لهذا البحث."
                )
                return

            await status.edit_text(
                f"📸 وجدت {len(images)} صور.\n"
                "⏳ أبدأ إضافتها إلى صور الملف الشخصي..."
            )

            success = 0
            failed = 0
            used_sources: dict[str, int] = {}

            for index, item in enumerate(images, start=1):
                path: Path | None = None

                try:
                    suffix = ".jpg"

                    url_lower = item["url"].lower()

                    if ".png" in url_lower:
                        suffix = ".png"
                    elif ".webp" in url_lower:
                        suffix = ".webp"
                    elif ".jpeg" in url_lower:
                        suffix = ".jpeg"

                    fd, temp_name = tempfile.mkstemp(
                        prefix=f"auto_{index}_",
                        suffix=suffix,
                        dir=DOWNLOAD_DIR,
                    )
                    os.close(fd)

                    path = Path(temp_name)

                    await status.edit_text(
                        f"⬇️ الصورة {index}/{len(images)}..."
                    )

                    await async_download(
                        item["url"],
                        path,
                    )

                    await status.edit_text(
                        f"⬆️ إضافة الصورة {index}/{len(images)}..."
                    )

                    await upload_as_profile_photo(path)

                    success += 1
                    src = item.get("source", "Unknown")
                    used_sources[src] = (
                        used_sources.get(src, 0) + 1
                    )

                except FloodWaitError as exc:
                    failed += 1
                    await status.edit_text(
                        f"⏳ Telegram فرض انتظار {exc.seconds} ثانية.\n"
                        f"تمت معالجة {success} صورة، "
                        f"وتوقفت العملية حفاظًا على الحساب."
                    )
                    logger.warning(
                        "Auto upload stopped by FloodWait."
                    )
                    break

                except Exception as exc:
                    failed += 1
                    logger.exception(
                        "فشل في الصورة %s: %s",
                        index,
                        exc,
                    )

                finally:
                    if path:
                        path.unlink(missing_ok=True)

            sources_text = ", ".join(
                f"{name}: {num}"
                for name, num in used_sources.items()
            ) or "غير معروف"

            await status.edit_text(
                "✅ انتهت العملية.\n\n"
                f"تمت الإضافة: {success}\n"
                f"فشل/تخطي: {failed}\n"
                f"المصدر: {sources_text}"
            )

        except Exception as exc:
            logger.exception(
                "Auto process failed: %s",
                exc,
            )
            await status.edit_text(
                "❌ حدث خطأ أثناء البحث أو التحميل.\n"
                "راجع سجل التشغيل لمعرفة التفاصيل."
            )


# ============================================================
# COMMANDS
# ============================================================
@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    if not is_owner(message):
        return await reject_non_owner(message)

    await message.answer(
        "✅ البوت جاهز.\n\n"
        "📷 أرسل صورة وسأضيفها إلى صور ملفك الشخصي.\n\n"
        "🤖 الوضع التلقائي:\n"
        "/auto 10\n"
        "يجلب 10 صور جمالية بشكل تلقائي.\n\n"
        "/auto 10 nature\n"
        "يجلب 10 صور حسب كلمة البحث.\n\n"
        "/status - حالة الاتصال\n"
        "/id - Telegram ID"
    )


@dp.message(Command("id"))
async def id_handler(message: Message) -> None:
    if not message.from_user:
        return

    await message.answer(
        f"Telegram ID: {message.from_user.id}"
    )


@dp.message(Command("status"))
async def status_handler(message: Message) -> None:
    if not is_owner(message):
        return await reject_non_owner(message)

    connected = user_client.is_connected()

    try:
        me = await user_client.get_me()

        account_name = " ".join(
            x for x in [me.first_name, me.last_name] if x
        ).strip() or "بدون اسم"

        username = (
            f"@{me.username}"
            if me.username
            else "بدون username"
        )

        source = "Pixabay → Wikimedia" if PIXABAY_API_KEY else "Wikimedia Commons"

        await message.answer(
            "🟢 حالة البوت: يعمل\n"
            f"🟢 اتصال الحساب: "
            f"{'متصل' if connected else 'غير متصل'}\n"
            f"👤 الحساب: {account_name}\n"
            f"🔹 Username: {username}\n"
            f"🆔 ID: {me.id}\n"
            f"🖼 مصدر البحث: {source}"
        )

    except Exception as exc:
        logger.exception(
            "فشل فحص الحساب: %s",
            exc,
        )
        await message.answer(
            "⚠️ تعذر الحصول على حالة الحساب الآن."
        )


def parse_auto_command(text: str) -> tuple[int, str]:
    """
    الصيغة:
      /auto 10
      /auto 10 nature
    """
    parts = text.strip().split(maxsplit=2)

    if len(parts) < 2:
        raise ValueError(
            "الاستخدام:\n"
            "/auto 10\n"
            "أو\n"
            "/auto 10 nature"
        )

    try:
        count = int(parts[1])
    except ValueError:
        raise ValueError(
            "عدد الصور يجب أن يكون رقمًا، مثل: /auto 10"
        )

    if not 1 <= count <= MAX_AUTO_IMAGES:
        raise ValueError(
            f"عدد الصور يجب أن يكون بين 1 و {MAX_AUTO_IMAGES}."
        )

    if len(parts) == 3:
        query = parts[2].strip()
    else:
        query = random.choice(DEFAULT_QUERIES)

    if not query:
        query = random.choice(DEFAULT_QUERIES)

    return count, clean_query(query)


@dp.message(Command("auto"))
async def auto_handler(message: Message) -> None:
    if not is_owner(message):
        return await reject_non_owner(message)

    try:
        count, query = parse_auto_command(
            message.text or ""
        )
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    await message.answer(
        f"🔎 البحث عن {count} صور لـ: {query}\n"
        "سأضيفها بالتتابع إلى صور ملفك الشخصي."
    )

    await process_auto_images(
        message,
        count,
        query,
    )


# ============================================================
# MANUAL IMAGE HANDLERS
# ============================================================
@dp.message(F.photo)
async def photo_handler(message: Message) -> None:
    if not is_owner(message):
        return await reject_non_owner(message)

    await process_manual_image(message)


@dp.message(F.document)
async def document_image_handler(message: Message) -> None:
    if not is_owner(message):
        return await reject_non_owner(message)

    if not message.document:
        return

    if not (
        (message.document.mime_type or "")
        .lower()
        .startswith("image/")
    ):
        return

    await process_manual_image(message)


async def process_manual_image(message: Message) -> None:
    path: Path | None = None

    processing_message = await message.answer(
        "⏳ يتم رفع الصورة إلى الملف الشخصي..."
    )

    try:
        path, _ = await download_telegram_file(message)

        await upload_as_profile_photo(path)

        await processing_message.edit_text(
            "✅ تم إضافة الصورة إلى صور ملفك الشخصي."
        )

    except ValueError as exc:
        await processing_message.edit_text(
            f"❌ {exc}"
        )

    except FloodWaitError as exc:
        await processing_message.edit_text(
            "⏳ Telegram طلب الانتظار "
            f"{exc.seconds} ثانية."
        )

    except RPCError as exc:
        logger.exception(
            "Telegram RPC error: %s",
            exc,
        )
        await processing_message.edit_text(
            "❌ Telegram رفض رفع الصورة. "
            "جرّب صورة JPG/PNG أخرى."
        )

    except Exception as exc:
        logger.exception(
            "Unexpected manual upload error: %s",
            exc,
        )
        await processing_message.edit_text(
            "❌ حدث خطأ أثناء رفع الصورة."
        )

    finally:
        if path:
            path.unlink(missing_ok=True)


@dp.message()
async def fallback_handler(message: Message) -> None:
    if not is_owner(message):
        return await reject_non_owner(message)

    if message.chat.type == "private":
        await message.answer(
            "📷 أرسل صورة أو استخدم:\n"
            "/auto 10 nature"
        )


# ============================================================
# MAIN
# ============================================================
async def main() -> None:
    global OWNER_ID

    check_config()

    logger.info(
        "بدء تسجيل الدخول للحساب الشخصي..."
    )

    await user_client.start(
        phone=PHONE_NUMBER
    )

    me = await user_client.get_me()

    if not me:
        raise RuntimeError(
            "تعذر الحصول على معلومات الحساب الشخصي."
        )

    OWNER_ID = me.id

    account_name = " ".join(
        x for x in [me.first_name, me.last_name] if x
    ).strip() or "بدون اسم"

    logger.info(
        "الحساب: %s | ID=%s",
        account_name,
        OWNER_ID,
    )

    bot_info = await bot.get_me()

    logger.info(
        "البوت: @%s | ID=%s",
        bot_info.username,
        bot_info.id,
    )

    try:
        await dp.start_polling(bot)
    finally:
        await user_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("تم إيقاف البرنامج.")
