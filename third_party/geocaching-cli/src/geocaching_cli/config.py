"""Paths and credential resolution. Secrets never belong in the repo."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_DIR_NAME = "geocaching-cli"
CREDENTIALS_FILENAME = "credentials.json"
SESSION_FILENAME = "session.json"
DB_FILENAME = "caches.db"


def app_dir() -> Path:
    override = os.environ.get("GEOCACHING_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME}"


def db_path() -> Path:
    override = os.environ.get("GEOCACHING_DB")
    if override:
        return Path(override).expanduser()
    return app_dir() / DB_FILENAME


def credentials_path() -> Path:
    override = os.environ.get("GEOCACHING_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return app_dir() / CREDENTIALS_FILENAME


def session_path() -> Path:
    return app_dir() / SESSION_FILENAME


def ensure_app_dir() -> Path:
    directory = app_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


@dataclass
class Credentials:
    username: str | None = None
    password: str | None = None
    cookie: str | None = None
    cookie_name: str = "gspkauth"

    @property
    def has_password(self) -> bool:
        return bool(self.username and self.password)

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie)

    @property
    def configured(self) -> bool:
        return self.has_password or self.has_cookie


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else {}
    if isinstance(data, dict):
        return data
    return {}


def load_credentials() -> Credentials:
    """Resolve credentials from env, then the gitignored local file.

    Environment variables win so CI/tests never need a file on disk.
    """
    creds = Credentials(
        username=os.environ.get("GEOCACHING_USERNAME") or None,
        password=os.environ.get("GEOCACHING_PASSWORD") or None,
        cookie=os.environ.get("GEOCACHING_COOKIE") or None,
        cookie_name=os.environ.get("GEOCACHING_COOKIE_NAME") or "gspkauth",
    )
    path = credentials_path()
    if path.is_file():
        try:
            data = _read_json(path)
        except (OSError, json.JSONDecodeError):
            data = {}
        creds.username = creds.username or data.get("username")
        creds.password = creds.password or data.get("password")
        creds.cookie = creds.cookie or data.get("cookie")
        creds.cookie_name = data.get("cookie_name") or creds.cookie_name
    return creds


def save_credentials(creds: Credentials) -> Path:
    ensure_app_dir()
    path = credentials_path()
    payload: dict[str, str] = {}
    if creds.username:
        payload["username"] = creds.username
    if creds.password:
        payload["password"] = creds.password
    if creds.cookie:
        payload["cookie"] = creds.cookie
    if creds.cookie_name and creds.cookie_name != "gspkauth":
        payload["cookie_name"] = creds.cookie_name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_credentials(*, forget_file: bool = True) -> None:
    if forget_file:
        path = credentials_path()
        if path.is_file():
            path.unlink()


def load_session() -> dict[str, Any] | None:
    path = session_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_session(payload: dict[str, Any]) -> Path:
    ensure_app_dir()
    path = session_path()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_session() -> None:
    path = session_path()
    if path.is_file():
        path.unlink()
