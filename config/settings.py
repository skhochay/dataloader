"""Django settings for dataloader. Override anything via environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Stage, processing, archive and fail folders live under here.
DATA_DIR = Path(os.environ.get("DATALOADER_DATA_DIR", r"C:\DataLoader"))

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
# Saved database passwords are encrypted with a key derived from this. Changing it makes
# saved passwords unreadable (they must be entered again).
VAULT_KEY = os.environ.get("DATALOADER_VAULT_KEY", SECRET_KEY)
ALLOWED_HOSTS = [
    h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "loader",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    # Fine for development. The engine and web server are separate processes
    # writing to one file, so prefer PostgreSQL once real volume arrives.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # WAL lets several processes read while one writes; IMMEDIATE makes a writer wait its turn instead of failing.
            "OPTIONS": {"timeout": 30, "init_command": "PRAGMA journal_mode=WAL;", "transaction_mode": "IMMEDIATE"},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "static/"

# ---------------------------------------------------------------------------
# File-drop engine. All four directories must live on the SAME filesystem so
# that moving a file between them is an atomic rename.
# ---------------------------------------------------------------------------
DATALOADER = {
    "HEARTBEAT_FILE": DATA_DIR / "engine.heartbeat",
    "POLL_INTERVAL": float(os.environ.get("DATALOADER_POLL_INTERVAL", "5")),
    # Before loading, each file is probed: it must be older than SETTLE_SECONDS,
    # and its size and modified time must be identical before and after a
    # PROBE_SECONDS wait (and no other process may hold it open). Files still
    # being copied in are skipped and re-checked on the next scan.
    "SETTLE_SECONDS": float(os.environ.get("DATALOADER_SETTLE_SECONDS", "2")),
    "PROBE_SECONDS": float(os.environ.get("DATALOADER_PROBE_SECONDS", "2")),
    "BATCH_SIZE": int(os.environ.get("DATALOADER_BATCH_SIZE", "1000")),
    # Send a file with already-loaded contents (same SHA-256) to fail.
    "REJECT_DUPLICATES": os.environ.get("DATALOADER_REJECT_DUPLICATES", "1") == "1",
    # Worker processes that load files in parallel. 0 = the engine loads files itself, one at a time.
    "WORKERS": int(os.environ.get("DATALOADER_WORKERS", "1")),
    # A file that hits a transient error (lost connection, deadlock, locked file) is retried this many times in all.
    "MAX_ATTEMPTS": int(os.environ.get("DATALOADER_MAX_ATTEMPTS", "3")),
}

# The first run creates this profile; more are added on the Directories page.
# Stage = drop files here; processing = file being loaded; archive = loaded; fail = failed (+ .error.txt).
DEFAULT_PROFILE = {
    "name": "Default",
    "stage_dir": str(DATA_DIR / "stage"),
    "processing_dir": str(DATA_DIR / "processing"),
    "archive_dir": str(DATA_DIR / "archive"),
    "fail_dir": str(DATA_DIR / "fail"),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {"loader": {"handlers": ["console"], "level": "INFO"}},
}
