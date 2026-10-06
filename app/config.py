"""Settings from the environment (.env locally). Read once, passed around."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    # Meta app secret: signs every webhook POST (X-Hub-Signature-256).
    whatsapp_app_secret: str
    # Any string we choose; Meta echoes it back during webhook verification.
    whatsapp_verify_token: str
    # Empty = dry run: replies are stored as not_sent instead of going to Meta.
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str
    # How long to wait for more messages before answering a burst (§5: 6-8 s).
    debounce_seconds: float
    # Supabase Storage holds listing photos (private bucket, service key).
    supabase_url: str
    supabase_service_role_key: str

    @property
    def dry_run(self) -> bool:
        return not (self.whatsapp_access_token and self.whatsapp_phone_number_id)


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    return Settings(
        database_url=env("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"),
        redis_url=env("REDIS_URL", "redis://127.0.0.1:6379/0"),
        whatsapp_app_secret=env("WHATSAPP_APP_SECRET", ""),
        whatsapp_verify_token=env("WHATSAPP_VERIFY_TOKEN", ""),
        whatsapp_access_token=env("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_api_version=env("WHATSAPP_API_VERSION", "v23.0"),
        debounce_seconds=float(env("DEBOUNCE_SECONDS", "7")),
        supabase_url=env("SUPABASE_URL", "http://127.0.0.1:54321"),
        supabase_service_role_key=env("SUPABASE_SERVICE_ROLE_KEY", ""),
    )
