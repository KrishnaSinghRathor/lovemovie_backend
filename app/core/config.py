import os
from dataclasses import dataclass, field


def _origins():
    return {o for o in os.getenv("ALLOWED_ORIGINS", "https://lovemovie-one.vercel.app").split(",") if o}


@dataclass
class Settings:
    mongo_url: str = field(default_factory=lambda: os.getenv("MONGO_URL", "mongodb+srv://krishnakumpawat197_db_user:rK2lVHQAdHsgb0T6@cluster0.be7vlz4.mongodb.net"))
    db_name: str = field(default_factory=lambda: os.getenv("MONGO_DB", "synccinema"))
    storage_dir: str = field(default_factory=lambda: os.getenv("STORAGE_DIR", "./data/movies"))
    max_upload_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(8 * 1024**3))))
    cookie_secure: bool = field(default_factory=lambda: os.getenv("COOKIE_SECURE", "false").lower() == "true")
    allowed_origins: set = field(default_factory=_origins)
    cookie_samesite: str = field(default_factory=lambda: os.getenv("COOKIE_SAMESITE", "lax").lower())  # "none" when frontend and API are on different sites
    session_ttl: int = 7 * 86400
    login_max_attempts: int = 5
    login_window: int = 60
