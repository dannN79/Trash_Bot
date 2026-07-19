import os
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

_db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
if _db_url.startswith("postgresql://") or _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("postgres://", "postgresql+asyncpg://", 1)
    parsed = urlparse(_db_url)
    params = parse_qs(parsed.query)
    params.pop("sslmode", None)
    new_query = urlencode({k: v[0] for k, v in params.items()})
    _db_url = urlunparse(parsed._replace(query=new_query))
DATABASE_URL = _db_url

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split())) if os.getenv("ADMIN_IDS") else []
