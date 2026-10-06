"""Photos and videos of one listing, sent on WhatsApp.

Only the listing's own Zameen gallery (listing_media) and its own video links
are ever sent. Photos live in the private Storage bucket; Meta needs each one
uploaded once, then it is sent by id. Ids expire (about 30 days), so an id
older than MEDIA_ID_TTL is replaced by a fresh upload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from PIL import Image
from psycopg import AsyncConnection

from app.config import Settings
from app.sender import SendResult, send_image, send_text, upload_media

BUCKET = "listing-photos"
MEDIA_ID_TTL = timedelta(days=25)
# WhatsApp image messages accept only these (WebP is for stickers), max 5 MB:
# https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media
WHATSAPP_IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}
# WhatsApp has no albums: every photo is its own message, so a few at a time.
DEFAULT_PHOTOS = 4


async def media_for_listing(conn: AsyncConnection, listing_id: int) -> dict:
    """What can be sent for a listing: photo count and video links."""
    photos = (await (await conn.execute(
        "SELECT count(*) FROM listing_media WHERE listing_id = %s", (listing_id,)
    )).fetchone())[0]
    videos = (await (await conn.execute(
        "SELECT video_urls FROM listings WHERE id = %s", (listing_id,)
    )).fetchone())
    return {"listing_id": listing_id, "photo_count": photos,
            "video_urls": list(videos[0]) if videos else []}


@dataclass(frozen=True)
class MediaSent:
    kind: str                       # photo | video
    ref: str                        # storage path or video URL
    result: SendResult


async def send_listing_media(conn: AsyncConnection, settings: Settings, to: str, listing_id: int,
                             *, photos: int = DEFAULT_PHOTOS, videos: bool = True,
                             skip_photos: int = 0) -> list[MediaSent]:
    """Send up to `photos` photos in gallery order (after `skip_photos` already
    sent), then the listing's video links. The caller records the messages."""
    rows = await (await conn.execute(
        """SELECT asset_id, storage_path, meta_media_id, meta_uploaded_at
           FROM listing_media WHERE listing_id = %s ORDER BY seq OFFSET %s LIMIT %s""",
        (listing_id, skip_photos, photos),
    )).fetchall()
    sent: list[MediaSent] = []
    for asset_id, path, media_id, uploaded_at in rows:
        if settings.dry_run:
            sent.append(MediaSent("photo", path, SendResult("not_sent", error="dry run")))
            continue
        try:
            if not media_id or not uploaded_at or datetime.now(timezone.utc) - uploaded_at > MEDIA_ID_TTL:
                content, mime, name = for_whatsapp(await _download(settings, path), path)
                media_id = await upload_media(settings, content, mime, name)
                await conn.execute(
                    """UPDATE listing_media SET meta_media_id = %s, meta_uploaded_at = now()
                       WHERE listing_id = %s AND asset_id = %s""",
                    (media_id, listing_id, asset_id),
                )
            sent.append(MediaSent("photo", path, await send_image(settings, to, media_id)))
        except (httpx.HTTPError, RuntimeError) as err:
            sent.append(MediaSent("photo", path, SendResult("failed", error=f"{type(err).__name__}: {err}")))

    if videos:
        urls = (await (await conn.execute(
            "SELECT video_urls FROM listings WHERE id = %s", (listing_id,)
        )).fetchone())[0]
        for url in urls:
            sent.append(MediaSent("video", url, await send_text(settings, to, url, preview_url=True)))
    return sent


def for_whatsapp(content: bytes, name: str) -> tuple[bytes, str, str]:
    """-> (bytes, mime, file name) in a format WhatsApp accepts for images.
    The format is read from the file itself, not from its name."""
    with Image.open(io.BytesIO(content)) as img:
        if img.format in WHATSAPP_IMAGE_TYPES:
            return content, WHATSAPP_IMAGE_TYPES[img.format], name
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue(), "image/jpeg", name.rsplit(".", 1)[0] + ".jpg"


async def _download(settings: Settings, path: str) -> bytes:
    key = settings.supabase_service_role_key
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{settings.supabase_url}/storage/v1/object/{BUCKET}/{path}",
            headers={"Authorization": f"Bearer {key}", "apikey": key},
        )
    resp.raise_for_status()
    return resp.content
