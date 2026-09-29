"""Settings for the Shipshape hackathon portal.

Everything is configured from environment variables with safe defaults, so
`docker compose up` works with no .env file and nothing reaches the network.
"""

import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default):
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_secret_key():
    """Use SECRET_KEY if given, otherwise generate one per install and keep it.

    No secret is baked into the image or the repo. The key lives next to the
    database, so it survives restarts and disappears with `docker compose down -v`.
    """
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = DATA_DIR / "secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(50)
    path.write_text(key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


SECRET_KEY = load_secret_key()
DEBUG = env_bool("DEBUG", False)

# Demo mode seeds known passwords and fixed session cookies so the acceptance
# checker and human judges can get in. Never turn it on for a real event.
DEMO_MODE = env_bool("DEMO_MODE", False)
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "dogfood-demo")

SITE_NAME = "Shipshape"
FIXTURES_PATH = Path(os.environ.get("FIXTURES_PATH", BASE_DIR / "fixtures.json"))

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],0.0.0.0")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "events",
    "teams",
    "projects",
    "judging",
    "voting",
    "integrity",
    "api",
    "webhooks",
    "records",
    "embeds",
    "transfer",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.SessionActivityMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "portal.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "portal.urls"
WSGI_APPLICATION = "portal.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "portal.context.site",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "portal.sqlite3",
        "OPTIONS": {
            "timeout": 20,
            # WAL lets readers carry on while one writer commits. IMMEDIATE
            # takes the write lock at BEGIN, so two requests racing for the
            # last seat on a team, or for the deadline, queue up instead of
            # failing halfway through.
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
     "OPTIONS": {"user_attributes": ("email", "name")}},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

if TESTING:
    # Hashing is deliberately slow in production; tests only need it correct.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

TEST_RUNNER = "portal.testing.Runner"

# The acceptance checker sends "Cookie: session=...", so the cookie is named
# session. Sessions live in the database, which is what makes "sign out of
# other devices" possible.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
CSRF_FAILURE_VIEW = "portal.views.csrf_failure"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG

# Mail. Only the email-gated community vote sends any. With no EMAIL_HOST,
# messages are written to DATA_DIR/outbox (one file each) instead, so an
# offline laptop never tries to reach a mail server.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_TIMEOUT = 10
else:
    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = DATA_DIR / "outbox"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Shipshape <no-reply@shipshape.localhost>")

# Webhooks. Loopback destinations are refused unless allowed (demo mode allows
# them, for the built-in test receiver). SELF_URL is how the portal reaches
# itself from inside its own container or process.
WEBHOOK_ALLOW_LOCAL = env_bool("WEBHOOK_ALLOW_LOCAL", DEMO_MODE)
SELF_URL = os.environ.get("SELF_URL", "http://127.0.0.1:8080").rstrip("/")

MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"

# Uploads: images only, checked and re-encoded in projects/images.py.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_GALLERY_IMAGES = 8

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
