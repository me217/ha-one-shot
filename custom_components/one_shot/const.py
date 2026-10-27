"""Constants for One Shot."""

DOMAIN = "one_shot"
STORAGE_KEY = f"{DOMAIN}.watches"
STORAGE_VERSION = 1

CONF_ALLOWED_DOMAINS = "allowed_domains"
CONF_DEFAULT_EXPIRY_HOURS = "default_expiry_hours"
CONF_ALLOWED_USERS = "allowed_users"
CONF_ALLOW_ANONYMOUS = "allow_anonymous"
CONF_ADMIN_ONLY_DOMAINS = "admin_only_domains"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_LOG_REQUESTS = "log_requests"

# Deliberately excludes lock, alarm_control_panel, etc. The user can add them in options.
DEFAULT_ALLOWED_DOMAINS = [
    "cover",
    "light",
    "switch",
    "fan",
    "climate",
    "media_player",
    "vacuum",
    "valve",
    "humidifier",
    "scene",
    "script",
    "input_boolean",
    "notify",
]
DEFAULT_EXPIRY_HOURS = 24
MAX_EXPIRY_HOURS = 24 * 14
DEFAULT_NOTIFY_SERVICE = "persistent_notification.create"

PENDING_TTL_SECONDS = 300

EVENT_COMPLETED = f"{DOMAIN}_completed"
EVENT_EXPIRED = f"{DOMAIN}_expired"

HISTORY_LIMIT = 100
SIGNAL_UPDATED = f"{DOMAIN}_updated"
PANEL_URL_PATH = "one-shot"
PANEL_STATIC_URL = f"/{DOMAIN}_static"
VERSION = "0.8.1"
