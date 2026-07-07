from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig


SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
SPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class ArchivedDocument:
    pdf_path: Path
    metadata_path: Path
    user: str
    document_name: str


class ArchiveStore:
    def __init__(self, config: AppConfig):
        self.config = config

    def store(self, document: bytes | str | Path, metadata: dict[str, Any], moment: datetime | None = None) -> ArchivedDocument:
        now = moment or datetime.now(self.config.timezone)
        user = best_user(metadata)
        document_name = best_document_name(metadata)

        pdf_result = self._ensure_pdf(document, metadata)
        folder = self._target_folder(user, now)
        folder.mkdir(parents=True, exist_ok=True)

        timestamp = now.strftime("%Y%m%dT%H%M%S%f")
        filename = f"{sanitize(document_name)}-{timestamp}.pdf"
        pdf_path = unique_path(folder / filename)
        metadata_path = pdf_path.with_suffix(".json")

        if isinstance(pdf_result, bytes):
            pdf_path.write_bytes(pdf_result)
        else:
            import shutil
            shutil.copy(str(pdf_result), str(pdf_path))
            if str(pdf_result) != str(document):
                try:
                    Path(pdf_result).unlink()
                except Exception:
                    pass

        metadata_path.write_text(
            json.dumps(
                {
                    "archived_at": now.isoformat(),
                    "user": user,
                    "document_name": document_name,
                    "source": metadata,
                    "pdf_file": pdf_path.name,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return ArchivedDocument(pdf_path=pdf_path, metadata_path=metadata_path, user=user, document_name=document_name)

    def _target_folder(self, user: str, moment: datetime) -> Path:
        user_folder = sanitize(user)
        date_folder = moment.strftime("%Y-%m-%d")

        sub = self.config.matching_folder(user, moment)
        if sub:
            return self.config.archive_root / user_folder / date_folder / sanitize(sub)
        return self.config.archive_root / user_folder / date_folder

    def _ensure_pdf(self, document: bytes | str | Path, metadata: dict[str, Any]) -> bytes | Path:
        is_pdf = False
        if isinstance(document, bytes):
            is_pdf = document.startswith(b"%PDF")
        else:
            try:
                with open(document, "rb") as f:
                    header = f.read(4)
                    is_pdf = header.startswith(b"%PDF")
            except Exception:
                pass

        if is_pdf:
            if isinstance(document, bytes):
                return document
            else:
                return Path(document)

        document_format = metadata.get("document-format")
        if isinstance(document_format, list):
            document_format = document_format[0]

        if not self.config.pdf_converter_command:
            raise UnsupportedDocumentFormat(
                f"unsupported document format {document_format or 'unknown'}; configure clients to send PDF"
            )

        with tempfile.TemporaryDirectory(prefix="archive-printer-") as tmp:
            if isinstance(document, bytes):
                input_path = Path(tmp) / "input.bin"
                input_path.write_bytes(document)
                input_str = str(input_path)
            else:
                input_str = str(document)

            output_path = Path(tmp) / "output.pdf"
            command = [
                part.format(input=input_str, output=str(output_path))
                for part in shlex.split(self.config.pdf_converter_command)
            ]
            subprocess.run(command, check=True, timeout=120)
            return output_path.read_bytes()


class UnsupportedDocumentFormat(Exception):
    pass


def best_user(metadata: dict[str, Any]) -> str:
    for key in (
        "auth-user",
        "requesting-user-name",
        "job-originating-user-name",
        "document-natural-language",
        "user-name",
    ):
        value = metadata.get(key)
        if value:
            return str(value)
    return "unknown"


def best_document_name(metadata: dict[str, Any]) -> str:
    for key in ("document-name", "job-name", "job-originating-document-name"):
        value = metadata.get(key)
        if value:
            return str(value)
    return "untitled"


def sanitize(value: str) -> str:
    text = SAFE_CHARS.sub("_", value).strip(" ._")
    text = SPACE_RUN.sub(" ", text)
    return text[:120] or "unknown"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for counter in range(1, 10000):
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a unique filename for {path}")
