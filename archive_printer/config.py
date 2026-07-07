from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


WEEKDAY_ALIASES = {
    "weekday": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
}

DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class TimetableRule:
    folder: str
    start: time
    end: time
    days: set[int] = field(default_factory=lambda: set[int]())
    users: set[str] = field(default_factory=lambda: set[str]())

    @classmethod
    def from_mapping(cls, item: dict[str, Any]) -> "TimetableRule":
        if "folder" not in item:
            raise ValueError("timetable rule is missing 'folder'")
        if "start" not in item or "end" not in item:
            raise ValueError(f"timetable rule for {item['folder']!r} needs start and end")

        days = _parse_days(item.get("days", ["weekday"]))
        users_value = item.get("users", item.get("user", ["*"]))
        if isinstance(users_value, str):
            users = {users_value.casefold()}
        else:
            users = {str(user).casefold() for user in users_value}

        return cls(
            folder=str(item["folder"]),
            start=_parse_time(str(item["start"])),
            end=_parse_time(str(item["end"])),
            days=days,
            users=users,
        )

    def matches(self, user: str, moment: datetime) -> bool:
        if moment.weekday() not in self.days:
            return False
        if "*" not in self.users and user.casefold() not in self.users:
            return False

        current = moment.time()
        if self.start <= self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


@dataclass(frozen=True)
class AppConfig:
    archive_root: Path
    timezone: ZoneInfo
    timetable: list[TimetableRule] = field(default_factory=lambda: list[TimetableRule]())
    bind_host: str = "0.0.0.0"
    port: int = 8631
    printer_name: str = "Archive Printer"
    require_basic_auth: bool = False
    pdf_converter_command: str | None = None
    enable_mdns: bool = True
    mdns_host: str | None = None
    mdns_address: str | None = None
    enable_tls: bool = False
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    use_letsencrypt: bool = False
    letsencrypt_email: str | None = None
    letsencrypt_domain: str | None = None
    letsencrypt_dns_provider: str = "manual"
    letsencrypt_dns_credentials_file: str | None = None
    low_disk_space_threshold_mb: int = 1000
    web_ui_domain: str | None = None
    users: dict[str, dict[str, str]] = field(default_factory=lambda: dict[str, dict[str, str]]())
    config_path: Path = Path("/config/config.json")

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "AppConfig":
        config_path = Path(path or os.environ.get("ARCHIVE_PRINTER_CONFIG", "/config/config.json"))
        data: dict[str, Any] = {}
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))

        timezone_name = str(data.get("timezone", os.environ.get("TZ", "UTC")))
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = ZoneInfo("UTC")

        archive_root = Path(str(data.get("archive_root", os.environ.get("ARCHIVE_ROOT", "/archive"))))
        timetable = [TimetableRule.from_mapping(item) for item in data.get("timetable", [])]

        return cls(
            archive_root=archive_root,
            timezone=tz,
            timetable=timetable,
            bind_host=str(data.get("bind_host", os.environ.get("BIND_HOST", "0.0.0.0"))),
            port=int(data.get("port", os.environ.get("PORT", "8631"))),
            printer_name=str(data.get("printer_name", os.environ.get("PRINTER_NAME", "Archive Printer"))),
            require_basic_auth=_bool(data.get("require_basic_auth", os.environ.get("REQUIRE_BASIC_AUTH", False))),
            pdf_converter_command=_none_if_blank(
                data.get("pdf_converter_command", os.environ.get("PDF_CONVERTER_COMMAND"))
            ),
            enable_mdns=_bool(data.get("enable_mdns", os.environ.get("ENABLE_MDNS", True))),
            mdns_host=_none_if_blank(data.get("mdns_host", os.environ.get("MDNS_HOST"))),
            mdns_address=_none_if_blank(data.get("mdns_address", os.environ.get("MDNS_ADDRESS"))),
            enable_tls=_bool(data.get("enable_tls", os.environ.get("ENABLE_TLS", False))),
            ssl_certfile=_none_if_blank(data.get("ssl_certfile", os.environ.get("SSL_CERTFILE"))),
            ssl_keyfile=_none_if_blank(data.get("ssl_keyfile", os.environ.get("SSL_KEYFILE"))),
            use_letsencrypt=_bool(data.get("use_letsencrypt", os.environ.get("USE_LETSENCRYPT", False))),
            letsencrypt_email=_none_if_blank(data.get("letsencrypt_email", os.environ.get("LETSENCRYPT_EMAIL"))),
            letsencrypt_domain=_none_if_blank(data.get("letsencrypt_domain", os.environ.get("LETSENCRYPT_DOMAIN"))),
            letsencrypt_dns_provider=str(data.get("letsencrypt_dns_provider", os.environ.get("LETSENCRYPT_DNS_PROVIDER", "manual"))),
            letsencrypt_dns_credentials_file=_none_if_blank(
                data.get("letsencrypt_dns_credentials_file", os.environ.get("LETSENCRYPT_DNS_CREDENTIALS_FILE"))
            ),
            low_disk_space_threshold_mb=int(data.get("low_disk_space_threshold_mb", os.environ.get("LOW_DISK_SPACE_THRESHOLD_MB", "1000"))),
            web_ui_domain=_none_if_blank(data.get("web_ui_domain", os.environ.get("WEB_UI_DOMAIN"))),
            users=data.get("users", {}),
            config_path=config_path,
        )

    def matching_folder(self, user: str, moment: datetime) -> str | None:
        for rule in self.timetable:
            if rule.matches(user, moment):
                return rule.folder
        return None


def _parse_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid time {value!r}; use HH:MM or HH:MM:SS") from exc


def _parse_days(values: Any) -> set[int]:
    if isinstance(values, str):
        values = [values]
    days: set[int] = set()
    for value in values:
        name = str(value).casefold()
        if name in WEEKDAY_ALIASES:
            days.update(WEEKDAY_ALIASES[name])
        elif name in DAY_NAMES:
            days.add(DAY_NAMES[name])
        else:
            raise ValueError(f"invalid day {value!r}")
    return days


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
