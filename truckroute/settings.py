import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-development-only-change-before-deployment")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
INSTALLED_APPS = ["django.contrib.contenttypes", "geocoding", "fuel_optimization"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
ROOT_URLCONF = "truckroute.urls"
WSGI_APPLICATION = "truckroute.wsgi.application"
if os.getenv("DATABASE_ENGINE", "postgresql") == "sqlite":
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "truckroute"),
            "USER": os.getenv("POSTGRES_USER", "truckrouter"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.getenv("POSTGRES_PORT", "55432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"connect_timeout": 10},
        }
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 100_000

# One OSRM route call per optimization. No station routing/table/matrix requests.
FUEL_OSRM_BASE_URL = os.getenv("FUEL_OSRM_BASE_URL", "https://router.project-osrm.org")
FUEL_OSRM_TIMEOUT = float(os.getenv("FUEL_OSRM_TIMEOUT", "15"))
FUEL_OSRM_MAX_RESPONSE_BYTES = 8_000_000
FUEL_MAX_ROUTE_POINTS = 100_000
FUEL_CORRIDOR_MILES = float(os.getenv("FUEL_CORRIDOR_MILES", "5"))
FUEL_DETOUR_MULTIPLIER = float(os.getenv("FUEL_DETOUR_MULTIPLIER", "1.4"))
FUEL_MAX_CANDIDATES = int(os.getenv("FUEL_MAX_CANDIDATES", "1000"))
FUEL_MAX_SEARCH_STATES = int(
    os.getenv("FUEL_MAX_SEARCH_STATES", os.getenv("FUEL_MAX_SEARCH_LABELS", "250000"))
)
