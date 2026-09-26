"""Runtime configuration for the autopilot service.

Everything is read from the environment (optionally loaded from an env file
given by ``DZEN_AUTOPILOT_ENV``; defaults to ``/etc/dzen-autopilot.env`` when
it exists, otherwise ``.env`` in the working directory).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _load_env_file() -> None:
    explicit = os.getenv("DZEN_AUTOPILOT_ENV", "").strip()
    candidates = [explicit] if explicit else ["/etc/dzen-autopilot.env", ".env"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            load_dotenv(candidate, override=False)
            return


_load_env_file()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data/autopilot")))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Europe/Moscow"))

    # --- LLM ---------------------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    deepseek_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    # Main writing/research model. Anthropic keys default to Claude Opus 5;
    # other providers get a sensible default per provider (see llm.py).
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    # Cheaper model for bulk helper calls (image prompts, tag extraction).
    llm_model_fast: str = field(default_factory=lambda: _env("LLM_MODEL_FAST"))
    llm_effort: str = field(default_factory=lambda: _env("LLM_EFFORT", "medium"))

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))

    # --- Publishing schedule ----------------------------------------------
    window_start: str = field(default_factory=lambda: _env("PUBLISH_WINDOW_START", "08:00"))
    window_end: str = field(default_factory=lambda: _env("PUBLISH_WINDOW_END", "23:00"))
    # publish | draft | off
    publish_mode: str = field(default_factory=lambda: _env("PUBLISH_MODE", "publish"))
    report_hour: int = field(default_factory=lambda: _int("REPORT_HOUR", 21))
    # How many ready articles per project the generator keeps ahead.
    queue_depth: int = field(default_factory=lambda: _int("QUEUE_DEPTH", 4))
    # Research/topic/write calls are pure LLM+HTTP (no browser) and safe to
    # run concurrently across projects and articles — publishing stays
    # single-threaded (one real browser session; parallel UI automation
    # there would look like an anti-bot attack to Dzen, not a speed win).
    # A literal "30 at once" would just hammer the shared OpenRouter free
    # pool harder and produce more 429s; this caps it at something that
    # actually helps.
    write_concurrency: int = field(default_factory=lambda: _int("WRITE_CONCURRENCY", 6))

    # --- Article shape -----------------------------------------------------
    article_min_chars: int = field(default_factory=lambda: _int("ARTICLE_MIN_CHARS", 3000))
    article_target_chars: int = field(default_factory=lambda: _int("ARTICLE_TARGET_CHARS", 5500))
    article_max_chars: int = field(default_factory=lambda: _int("ARTICLE_MAX_CHARS", 9000))
    images_per_article: int = field(default_factory=lambda: _int("IMAGES_PER_ARTICLE", 2))

    # --- Dzen / browser ----------------------------------------------------
    profile_dir: Path = field(default_factory=lambda: Path(_env("BROWSER_PROFILE_DIR", "data/autopilot/profile")))
    dzen_publisher_id: str = field(default_factory=lambda: _env("DZEN_PUBLISHER_ID"))
    headless: bool = field(default_factory=lambda: _bool("HEADLESS", True))

    # --- Remote login (noVNC) ---------------------------------------------
    login_public_url: str = field(default_factory=lambda: _env("LOGIN_PUBLIC_URL"))
    login_vnc_port: int = field(default_factory=lambda: _int("LOGIN_VNC_PORT", 5901))
    login_novnc_port: int = field(default_factory=lambda: _int("LOGIN_NOVNC_PORT", 6080))
    login_display: str = field(default_factory=lambda: _env("LOGIN_DISPLAY", ":99"))
    novnc_web_root: str = field(default_factory=lambda: _env("NOVNC_WEB_ROOT", "/usr/share/novnc"))
    # Bind address for websockify so a docker-network Caddy (host.docker.internal
    # -> docker0 bridge) can reach it. Loopback-only if unset (no public route).
    login_bind_addr: str = field(default_factory=lambda: _env("LOGIN_BIND_ADDR", "127.0.0.1"))

    # --- Misc --------------------------------------------------------------
    http_proxy: str = field(default_factory=lambda: _env("OUTBOUND_PROXY"))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autopilot.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def llm_provider(self) -> str:
        if self.anthropic_api_key:
            return "anthropic"
        if self.openrouter_api_key:
            return "openrouter"
        if self.openai_api_key:
            return "openai"
        if self.deepseek_api_key:
            return "deepseek"
        return ""

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def problems(self) -> list[str]:
        out = []
        if not self.llm_provider:
            out.append("Нет LLM-ключа: задайте ANTHROPIC_API_KEY (или OPENROUTER_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY)")
        if not self.telegram_bot_token:
            out.append("Нет TELEGRAM_BOT_TOKEN")
        if self.publish_mode not in ("publish", "draft", "off"):
            out.append(f"PUBLISH_MODE должен быть publish|draft|off, сейчас {self.publish_mode!r}")
        return out


settings = Settings()
