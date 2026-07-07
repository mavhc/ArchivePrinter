from __future__ import annotations

import argparse
import base64
import logging
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
from typing import Any

from .archive import ArchiveStore, UnsupportedDocumentFormat
from .config import AppConfig
from .ipp import GroupTag, IppRequest, Operation, Status, ValueTag, parse_request, parse_request_stream, response
from .mdns import MdnsPublisher
class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 100):
        super().__init__()
        self.capacity = capacity
        self.records: list[str] = []
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            msg = self.format(record)
            with self._lock:
                self.records.append(msg)
                if len(self.records) > self.capacity:
                    self.records.pop(0)
        except Exception:
            self.handleError(record)

    def get_logs(self) -> list[str]:
        with self._lock:
            return list(self.records)


LOGGER = logging.getLogger("archive_printer")
LOG_HANDLER = InMemoryLogHandler()
LOG_HANDLER.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
LOGGER.addHandler(LOG_HANDLER)
ISO_A_MEDIA = [
    "iso_a0_841x1189mm",
    "iso_a1_594x841mm",
    "iso_a2_420x594mm",
    "iso_a3_297x420mm",
    "iso_a4_210x297mm",
    "iso_a5_148x210mm",
    "iso_a6_105x148mm",
    "iso_a7_74x105mm",
    "iso_a8_52x74mm",
    "iso_a9_37x52mm",
    "iso_a10_26x37mm",
]
ISO_B_MEDIA = [
    "iso_b0_1000x1414mm",
    "iso_b1_707x1000mm",
    "iso_b2_500x707mm",
    "iso_b3_353x500mm",
    "iso_b4_250x353mm",
    "iso_b5_176x250mm",
    "iso_b6_125x176mm",
    "iso_b7_88x125mm",
    "iso_b8_62x88mm",
    "iso_b9_44x62mm",
    "iso_b10_31x44mm",
]
SUPPORTED_MEDIA = ISO_A_MEDIA + ISO_B_MEDIA
DEFAULT_MEDIA = "iso_a4_210x297mm"


@dataclass
class Job:
    job_id: int
    name: str
    user: str
    state: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Subscription:
    subscription_id: int
    job_id: int | None
    recipient_uri: str
    events: list[str]
    lease_duration: int
    user: str
    created_at: float = field(default_factory=lambda: __import__("time").time())


class ArchivePrinterApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = ArchiveStore(config)
        self._lock = threading.RLock()
        self._next_job_id = 1
        self._jobs: dict[int, Job] = {}
        self._next_subscription_id = 1
        self._subscriptions: dict[int, Subscription] = {}
        self.free_mb: float = 0.0
        self.total_mb: float = 0.0
        self.days_remaining: float | None = None
        self.disk_warning: bool = False

    def start_disk_monitor(self) -> None:
        self._disk_monitor_stop = threading.Event()
        self._disk_monitor_thread = threading.Thread(
            target=self._disk_monitor_loop,
            daemon=True,
            name="DiskSpaceMonitor"
        )
        self._disk_monitor_thread.start()

    def shutdown(self) -> None:
        if hasattr(self, "_disk_monitor_stop"):
            self._disk_monitor_stop.set()
        if hasattr(self, "_disk_monitor_thread"):
            try:
                self._disk_monitor_thread.join(timeout=1.0)
            except Exception:
                pass

    def _disk_monitor_loop(self) -> None:
        check_interval = 86400
        time_since_last_check = check_interval
        while not self._disk_monitor_stop.is_set():
            if time_since_last_check >= check_interval:
                try:
                    self.check_disk_space()
                except Exception as exc:
                    LOGGER.error("Error in disk space monitor check: %s", exc)
                time_since_last_check = 0
            self._disk_monitor_stop.wait(5)
            time_since_last_check += 5

    def check_disk_space(self) -> None:
        import shutil
        import json
        from datetime import datetime, date

        archive_root = self.config.archive_root
        if not archive_root.exists():
            try:
                archive_root.mkdir(parents=True, exist_ok=True)
            except Exception:
                return

        try:
            usage = shutil.disk_usage(str(archive_root))
        except Exception as exc:
            LOGGER.warning("Could not get disk usage for %s: %s", archive_root, exc)
            return

        free_mb = usage.free / (1024 * 1024)
        total_mb = usage.total / (1024 * 1024)

        history_file = archive_root / "disk_space_history.json"
        history = []
        if history_file.exists():
            try:
                history = json.loads(history_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        today_str = date.today().isoformat()
        today_entry = {"date": today_str, "free_mb": free_mb}
        history = [item for item in history if item.get("date") != today_str]
        history.append(today_entry)
        history.sort(key=lambda x: x.get("date", ""))
        history = history[-90:]

        try:
            history_file.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except Exception as exc:
            LOGGER.warning("Could not write disk space history file: %s", exc)

        prediction_str = ""
        days_remaining = None
        if len(history) >= 2:
            from datetime import timedelta
            cutoff_date = (date.today() - timedelta(days=30)).isoformat()
            recent_history = [h for h in history if h.get("date", "") >= cutoff_date]
            if len(recent_history) >= 2:
                oldest = recent_history[0]
                newest = recent_history[-1]
                try:
                    d1 = date.fromisoformat(oldest["date"])
                    d2 = date.fromisoformat(newest["date"])
                    days_diff = (d2 - d1).days
                    if days_diff >= 1:
                        free_diff = oldest["free_mb"] - newest["free_mb"]
                        consumption_rate = free_diff / days_diff
                        if consumption_rate > 0:
                            days_remaining = free_mb / consumption_rate
                            prediction_str = f" At the current consumption rate of {consumption_rate:.1f} MB/day, approximately {days_remaining:.1f} days of space remain."
                        else:
                            prediction_str = " Disk space usage is stable or increasing."
                except Exception:
                    pass

        free_gb = free_mb / 1024
        total_gb = total_mb / 1024
        LOGGER.info("Disk Space Monitor: %.2f GB free of %.2f GB.%s", free_gb, total_gb, prediction_str)

        threshold_mb = self.config.low_disk_space_threshold_mb
        self.free_mb = free_mb
        self.total_mb = total_mb
        self.days_remaining = days_remaining
        self.disk_warning = free_mb < threshold_mb

        if self.disk_warning:
            pred_alert = f" Prediction: {days_remaining:.1f} days left." if days_remaining is not None else ""
            LOGGER.warning(
                "WARNING: Disk space is low! Only %.2f GB remaining (threshold: %.2f GB).%s",
                free_gb, threshold_mb / 1024, pred_alert
            )

    def next_job(self, request: IppRequest, extra_metadata: dict[str, Any]) -> Job:
        metadata = {**request.attributes, **extra_metadata}
        with self._lock:
            job_id = self._next_job_id
            self._next_job_id += 1
            job = Job(
                job_id=job_id,
                name=str(metadata.get("job-name") or metadata.get("document-name") or f"Job {job_id}"),
                user=str(metadata.get("auth-user") or metadata.get("requesting-user-name") or "unknown"),
                state=3,
                metadata={**metadata, "job-id": job_id},
            )
            self._jobs[job_id] = job
            self._prune_jobs()
            return job

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def _prune_jobs(self) -> None:
        with self._lock:
            if len(self._jobs) <= 1000:
                return

            all_ids = sorted(self._jobs.keys())
            completed_ids = [
                job_id for job_id in all_ids 
                if self._jobs[job_id].state in {7, 8, 9}
            ]

            to_prune = len(self._jobs) - 1000
            pruned_count = 0
            for job_id in completed_ids:
                if pruned_count >= to_prune:
                    break
                del self._jobs[job_id]
                pruned_count += 1

            remaining_to_prune = to_prune - pruned_count
            if remaining_to_prune > 0:
                all_ids = sorted(self._jobs.keys())
                for job_id in all_ids[:remaining_to_prune]:
                    del self._jobs[job_id]

    def load_persisted_jobs(self) -> None:
        import json
        from pathlib import Path

        if not self.config.archive_root.exists():
            return

        json_files = list(self.config.archive_root.rglob("*.json"))
        loaded_jobs = []

        for path in json_files:
            if path.name == "config.json" or ".ssl" in path.parts:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if "archived_at" not in data or "source" not in data:
                    continue

                source = data["source"]
                job_id = int(source.get("job-id", 0) or 0)

                job = Job(
                    job_id=job_id,
                    name=data.get("document_name") or f"Job {job_id}",
                    user=data.get("user") or "unknown",
                    state=9,
                    metadata={
                        **source,
                        "pdf_path": str(path.with_suffix(".pdf")),
                        "metadata_path": str(path)
                    },
                )
                loaded_jobs.append((job_id, job, path.stat().st_mtime))
            except Exception:
                continue

        loaded_jobs.sort(key=lambda x: x[2])

        with self._lock:
            for job_id, job, _ in loaded_jobs:
                if job_id <= 0 or job_id in self._jobs:
                    job_id = self._next_job_id
                    self._next_job_id += 1
                    job = Job(
                        job_id=job_id,
                        name=job.name,
                        user=job.user,
                        state=job.state,
                        metadata={**job.metadata, "job-id": job_id},
                    )
                else:
                    if job_id >= self._next_job_id:
                        self._next_job_id = job_id + 1

                self._jobs[job_id] = job

            self._prune_jobs()

    def next_subscription(self, request: IppRequest, job_id: int | None = None) -> Subscription:
        recipient_uri = str(request.attributes.get("notify-recipient-uri") or "ipp://localhost/notify")
        events_val = request.attributes.get("notify-events")
        if not events_val:
            events = ["job-state-changed" if job_id else "printer-state-changed"]
        elif isinstance(events_val, list):
            events = [str(e) for e in events_val]
        else:
            events = [str(events_val)]

        try:
            lease_duration = int(request.attributes.get("notify-lease-duration", 3600) or 3600)
        except Exception:
            lease_duration = 3600
        user = str(request.attributes.get("requesting-user-name") or "unknown")

        with self._lock:
            sub_id = self._next_subscription_id
            self._next_subscription_id += 1
            sub = Subscription(
                subscription_id=sub_id,
                job_id=job_id,
                recipient_uri=recipient_uri,
                events=events,
                lease_duration=lease_duration,
                user=user,
            )
            self._subscriptions[sub_id] = sub
            return sub

    def _prune_subscriptions(self) -> None:
        import time
        with self._lock:
            now = time.time()
            expired = [
                sub_id for sub_id, sub in self._subscriptions.items()
                if now - sub.created_at > sub.lease_duration
            ]
            for sub_id in expired:
                del self._subscriptions[sub_id]

    def get_subscription(self, sub_id: int) -> Subscription | None:
        with self._lock:
            self._prune_subscriptions()
            return self._subscriptions.get(sub_id)

    def subscriptions(self) -> list[Subscription]:
        with self._lock:
            self._prune_subscriptions()
            return list(self._subscriptions.values())

    def remove_subscription(self, sub_id: int) -> bool:
        with self._lock:
            self._prune_subscriptions()
            if sub_id in self._subscriptions:
                del self._subscriptions[sub_id]
                return True
            return False
            return False


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    if isinstance(val, list):
        if not val:
            return default
        val = val[0]
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _get_user_role(username: str, config: AppConfig) -> str:
    if username == "admin" or username == "administrator":
        return "administrator"
    if username in config.users:
        role = config.users[username].get("role", "student").lower()
        if role in {"administrator", "staff", "student"}:
            return role
    return "student"


def can_see_job(viewer_username: str, viewer_role: str, job_owner: str, config: AppConfig) -> bool:
    if viewer_role == "administrator":
        return True
    if viewer_role == "staff":
        owner_role = _get_user_role(job_owner, config)
        return owner_role == "student" or job_owner == viewer_username
    if viewer_role == "student":
        return job_owner == viewer_username
    return False


def can_delete_job(viewer_username: str, viewer_role: str, job_owner: str) -> bool:
    if viewer_role == "administrator":
        return True
    if viewer_role == "staff":
        return job_owner == viewer_username
    return False


def make_handler(app: ArchivePrinterApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArchivePrinterIPP/0.1"

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            # 1. Health check & IPP print endpoint (always open)
            if path == "/healthz":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return

            if path == "/ipp/print":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"archive-printer ipp server\n")
                return

            # 2. Check if this is a Web UI path
            is_web_ui = path == "/" or path.startswith("/jobs") or path == "/config"

            # 3. Domain alias validation
            if is_web_ui and app.config.web_ui_domain:
                host_header = self.headers.get("Host", "")
                host_name = host_header.split(":")[0].strip().lower()
                if host_name != app.config.web_ui_domain.lower():
                    if path == "/":
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(b"archive-printer ipp server\n")
                        return
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

            # 4. Handle Web UI paths
            if is_web_ui:
                auth_info = self._get_authenticated_user_and_role()
                if not auth_info:
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("WWW-Authenticate", 'Basic realm="Archive Printer Web UI"')
                    self.end_headers()
                    return
                username, role = auth_info

                if path == "/" or path == "/jobs":
                    msg = query.get("msg", [""])[0]
                    self._serve_dashboard(username, role, msg)
                    return

                import re
                match_download = re.match(r"^/jobs/(\d+)/download$", path)
                if match_download:
                    job_id = int(match_download.group(1))
                    inline = query.get("inline", ["false"])[0].lower() == "true"
                    self._serve_download(job_id, username, role, "pdf", inline=inline)
                    return

                match_metadata = re.match(r"^/jobs/(\d+)/metadata$", path)
                if match_metadata:
                    job_id = int(match_metadata.group(1))
                    self._serve_download(job_id, username, role, "json")
                    return

                match_delete = re.match(r"^/jobs/(\d+)/delete$", path)
                if match_delete:
                    job_id = int(match_delete.group(1))
                    self._handle_delete(job_id, username, role)
                    return

                match_detail = re.match(r"^/jobs/(\d+)$", path)
                if match_detail:
                    job_id = int(match_detail.group(1))
                    self._serve_job_detail(job_id, username, role)
                    return
                    
                if path == "/config":
                    if role == "administrator":
                        self._serve_config_editor()
                    else:
                        self.send_error(HTTPStatus.FORBIDDEN)
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if path == "/":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"archive-printer ipp server\n")
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def _get_authenticated_user_and_role(self) -> tuple[str, str] | None:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return None
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8", errors="replace")
            except Exception:
                return None
            username, _, password = decoded.partition(":")
            if not username:
                return None

            users = app.config.users
            if not users:
                if username == "admin" and password == "admin":
                    return ("admin", "administrator")
                return (username, "student")

            if username in users:
                user_info = users[username]
                expected_password = user_info.get("password")
                if expected_password is None or password == expected_password:
                    role = user_info.get("role", "student").lower()
                    if role not in {"administrator", "staff", "student"}:
                        role = "student"
                    return (username, role)

            return None

        def _serve_dashboard(self, username: str, role: str, message: str = "") -> None:
            with app._lock:
                all_jobs = list(app._jobs.values())

            visible_jobs = []
            for job in all_jobs:
                if can_see_job(username, role, job.user, app.config):
                    visible_jobs.append(job)

            visible_jobs.sort(key=lambda j: j.job_id, reverse=True)

            job_rows_html = ""
            for job in visible_jobs:
                pdf_path = job.metadata.get("pdf_path")
                size_str = "0 KB"
                if pdf_path:
                    try:
                        p = Path(pdf_path)
                        if p.exists():
                            size_bytes = p.stat().st_size
                            if size_bytes > 1024 * 1024:
                                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                            else:
                                size_str = f"{size_bytes / 1024:.1f} KB"
                    except Exception:
                        pass

                time_val = job.metadata.get("archived_at") or "Unknown"
                if "T" in time_val:
                    time_val = time_val.replace("T", " ")[:19]

                state_badge = ""
                if job.state in {7, 8}:
                    state_badge = '<span class="badge badge-red">Canceled</span>'
                elif job.state == 9:
                    state_badge = '<span class="badge badge-green">Completed</span>'
                else:
                    state_badge = '<span class="badge badge-blue">Pending</span>'

                owner_role = _get_user_role(job.user, app.config)
                role_badge = ""
                if owner_role == "administrator":
                    role_badge = '<span class="badge-role role-admin">Admin</span>'
                elif owner_role == "staff":
                    role_badge = '<span class="badge-role role-staff">Staff</span>'
                else:
                    role_badge = '<span class="badge-role role-student">Student</span>'

                actions = f'<a href="/jobs/{job.job_id}" class="btn btn-blue" id="btn-view-{job.job_id}">View</a> '
                actions += f'<a href="/jobs/{job.job_id}/download" class="btn btn-purple" id="btn-dl-{job.job_id}">PDF</a> '
                if can_delete_job(username, role, job.user):
                    actions += f'<a href="/jobs/{job.job_id}/delete" onclick="return confirm(\'Delete this job?\');" class="btn btn-red" id="btn-del-{job.job_id}">Delete</a>'

                job_rows_html += f"""
                <tr class="job-row">
                    <td onclick="event.stopPropagation();"><input type="checkbox" name="job_ids" value="{job.job_id}" class="job-checkbox" onclick="event.stopPropagation();"></td>
                    <td>#{job.job_id}</td>
                    <td class="font-semibold">{job.name}</td>
                    <td>{job.user} {role_badge}</td>
                    <td>{size_str}</td>
                    <td>{time_val}</td>
                    <td>{state_badge}</td>
                    <td class="text-right">{actions}</td>
                </tr>
                """

            if not job_rows_html:
                job_rows_html = '<tr><td colspan="8" class="text-center py-12" style="color: var(--text-secondary);">No print jobs found.</td></tr>'

            alert_html = ""
            if message:
                alert_html = f"""
                <div class="alert alert-success animate-fade mb-6" id="alert-msg">
                    <svg xmlns="http://www.w3.org/2000/svg" style="height: 1.5rem; width: 1.5rem; vertical-align: middle;" class="inline-block mr-2" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <span>{message}</span>
                </div>
                """

            # Disk Widget HTML
            disk_widget_html = ""
            if app.total_mb > 0:
                used_mb = app.total_mb - app.free_mb
                used_pct = (used_mb / app.total_mb) * 100
                free_gb = app.free_mb / 1024
                total_gb = app.total_mb / 1024
                
                status_text = "Storage Healthy"
                progress_class = "progress-green"
                if app.disk_warning:
                    status_text = "Storage Low!"
                    progress_class = "progress-red"

                pred_text = "Disk usage is stable."
                if app.days_remaining is not None:
                    pred_text = f"Approximately {app.days_remaining:.1f} days of space left."

                disk_widget_html = f"""
                <div class="disk-widget animate-fade mb-6">
                    <div class="disk-header">
                        <span class="disk-title">System Storage ({status_text})</span>
                        <span class="disk-details">{free_gb:.2f} GB free of {total_gb:.2f} GB</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill {progress_class}" style="width: {used_pct:.1f}%;"></div>
                    </div>
                    <div class="disk-footer">
                        <span>{pred_text}</span>
                    </div>
                </div>
                """

            # Log Viewer HTML for Administrators
            log_lines_html = ""
            if role == "administrator":
                logs = LOG_HANDLER.get_logs()
                import html as html_module
                log_content = "\\n".join(html_module.escape(l) for l in logs)
                log_lines_html = f"""
                <div class="glass-panel animate-fade" style="margin-top: 3rem; padding: 1.5rem;">
                    <h3 style="margin-top: 0; color: var(--accent-red); font-size: 1.25rem;">System Log Viewer</h3>
                    <pre style="background: #090d16; color: #34d399; font-family: monospace; padding: 1.25rem; border-radius: 10px; font-size: 0.85rem; max-height: 250px; overflow-y: auto; text-align: left; white-space: pre-wrap; margin-bottom: 0;">{log_content}</pre>
                </div>
                """

            # Config Link for Admins
            config_link_html = ""
            if role == "administrator":
                config_link_html = '<a href="/config" class="btn btn-purple" id="btn-goto-config">Edit Settings</a>'

            role_title = role.capitalize()
            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Archive Printer Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0b0f19;
            --bg-card: rgba(22, 28, 45, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --accent-blue: #3b82f6;
            --accent-purple: #8b5cf6;
            --accent-red: #ef4444;
            --accent-green: #10b981;
        }}
        body {{
            background: radial-gradient(circle at top, #1e1b4b, #0b0f19);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 3rem 1.5rem;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 3rem;
        }}
        h1 {{
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, #a78bfa, #3b82f6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
        }}
        .user-badge {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            padding: 0.5rem 1.2rem;
            border-radius: 9999px;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .role-indicator {{
            color: var(--accent-purple);
            font-weight: 600;
        }}
        .glass-panel {{
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 2rem;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.4s ease-out forwards;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .search-container {{
            margin-bottom: 2rem;
            position: relative;
        }}
        .search-input {{
            width: 100%;
            padding: 1rem 1.5rem;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-primary);
            font-family: inherit;
            font-size: 1rem;
            box-sizing: border-box;
            transition: border-color 0.2s, box-shadow 0.2s;
        }}
        .search-input:focus {{
            outline: none;
            border-color: var(--accent-blue);
            box-shadow: 0 0 15px rgba(59, 130, 246, 0.3);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }}
        th {{
            padding: 1.2rem 1.5rem;
            font-weight: 600;
            color: var(--text-secondary);
            border-bottom: 2px solid var(--border-color);
            font-size: 0.95rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        td {{
            padding: 1.2rem 1.5rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 1rem;
        }}
        .job-row {{
            transition: background-color 0.2s;
        }}
        .job-row:hover {{
            background: rgba(255, 255, 255, 0.02);
        }}
        .font-semibold {{
            font-weight: 600;
        }}
        .badge {{
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
            display: inline-block;
        }}
        .badge-green {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge-blue {{ background: rgba(59, 130, 246, 0.15); color: var(--accent-blue); border: 1px solid rgba(59, 130, 246, 0.3); }}
        .badge-red {{ background: rgba(239, 68, 68, 0.15); color: var(--accent-red); border: 1px solid rgba(239, 68, 68, 0.3); }}
        
        .badge-role {{
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            margin-left: 0.5rem;
            text-transform: uppercase;
        }}
        .role-admin {{ background: rgba(239, 68, 68, 0.12); color: var(--accent-red); }}
        .role-staff {{ background: rgba(139, 92, 246, 0.12); color: var(--accent-purple); }}
        .role-student {{ background: rgba(59, 130, 246, 0.12); color: var(--accent-blue); }}

        .btn {{
            padding: 0.4rem 0.9rem;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            text-decoration: none;
            display: inline-block;
            transition: transform 0.2s, box-shadow 0.2s;
            cursor: pointer;
            border: none;
        }}
        .btn:hover {{
            transform: translateY(-1px);
        }}
        .btn-blue {{ background: var(--accent-blue); color: #fff; }}
        .btn-blue:hover {{ box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }}
        .btn-purple {{ background: var(--accent-purple); color: #fff; }}
        .btn-purple:hover {{ box-shadow: 0 4px 12px rgba(139, 92, 246, 0.3); }}
        .btn-red {{ background: var(--accent-red); color: #fff; }}
        .btn-red:hover {{ box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3); }}
        
        .alert {{
            padding: 1rem 1.5rem;
            border-radius: 12px;
            font-weight: 600;
            display: flex;
            align-items: center;
        }}
        .alert-success {{
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }}
        .text-right {{ text-align: right; }}
        .text-center {{ text-align: center; }}
        .py-12 {{ padding-top: 3rem; padding-bottom: 3rem; }}
        .mb-6 {{ margin-bottom: 1.5rem; }}
        .inline-block {{ display: inline-block; }}
        .mr-2 {{ margin-right: 0.5rem; }}

        /* Disk Widget Styles */
        .disk-widget {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1.25rem 1.5rem;
        }}
        .disk-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }}
        .disk-title {{
            font-weight: 600;
            font-size: 0.95rem;
        }}
        .disk-details {{
            color: var(--text-secondary);
            font-size: 0.9rem;
        }}
        .progress-bar-bg {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 9999px;
            height: 8px;
            overflow: hidden;
            margin-bottom: 0.75rem;
        }}
        .progress-bar-fill {{
            height: 100%;
            border-radius: 9999px;
            transition: width 0.3s ease;
        }}
        .progress-green {{ background: linear-gradient(90deg, var(--accent-blue), var(--accent-green)); }}
        .progress-red {{ background: linear-gradient(90deg, var(--accent-purple), var(--accent-red)); }}
        .disk-footer {{
            font-size: 0.85rem;
            color: var(--text-secondary);
        }}
        .bulk-actions {{
            display: flex;
            gap: 1rem;
            align-items: center;
            margin-bottom: 1.5rem;
            background: rgba(255, 255, 255, 0.02);
            padding: 0.75rem 1.25rem;
            border-radius: 10px;
            border: 1px solid var(--border-color);
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Archive Printer</h1>
            <div style="display: flex; gap: 1rem; align-items: center;">
                {config_link_html}
                <div class="user-badge" id="user-badge-info">
                    <span>Logged in as <strong>{username}</strong></span>
                    <span class="role-indicator">({role_title})</span>
                </div>
            </div>
        </header>

        {alert_html}
        {disk_widget_html}

        <div class="glass-panel">
            <form id="bulk-form" method="POST" action="/jobs/bulk">
                <input type="hidden" name="bulk_action" id="bulk-action-input">
                
                <div class="bulk-actions">
                    <span style="font-size: 0.9rem; font-weight: 600; color: var(--text-secondary); margin-right: 0.5rem;">Bulk Actions:</span>
                    <button type="button" class="btn btn-purple" id="btn-bulk-zip" onclick="submitBulk('zip')">Export Selected (ZIP)</button>
                    <button type="button" class="btn btn-red" id="btn-bulk-delete" onclick="submitBulk('delete')">Delete Selected</button>
                </div>

                <div class="search-container">
                    <input type="text" id="search-box" class="search-input" placeholder="Search by job name or username..." oninput="filterJobs()">
                </div>
                
                <div style="overflow-x: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 30px;"><input type="checkbox" id="select-all" onclick="toggleSelectAll(this)"></th>
                                <th>ID</th>
                                <th>Job Name</th>
                                <th>Owner</th>
                                <th>Size</th>
                                <th>Date</th>
                                <th>Status</th>
                                <th class="text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="jobs-tbody">
                            {job_rows_html}
                        </tbody>
                    </table>
                </div>
            </form>
        </div>

        {log_lines_html}
    </div>

    <script>
        function filterJobs() {{
            const query = document.getElementById('search-box').value.toLowerCase().trim();
            const rows = document.querySelectorAll('.job-row');
            rows.forEach(row => {{
                const cells = row.getElementsByTagName('td');
                if (cells.length >= 4) {{
                    const jobName = cells[2].textContent.toLowerCase();
                    const userName = cells[3].textContent.toLowerCase();
                    if (jobName.includes(query) || userName.includes(query)) {{
                        row.style.display = '';
                    }} else {{
                        row.style.display = 'none';
                    }}
                }}
            }});
        }}

        function toggleSelectAll(master) {{
            const checkboxes = document.querySelectorAll('.job-checkbox');
            checkboxes.forEach(cb => {{
                cb.checked = master.checked;
            }});
        }}

        function submitBulk(action) {{
            const checkboxes = document.querySelectorAll('.job-checkbox:checked');
            if (checkboxes.length === 0) {{
                alert('Please select at least one print job.');
                return;
            }}
            if (action === 'delete' && !confirm('Are you sure you want to delete the selected print jobs?')) {{
                return;
            }}
            document.getElementById('bulk-action-input').value = action;
            document.getElementById('bulk-form').submit();
        }}
    </script>
</body>
</html>
"""
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _serve_job_detail(self, job_id: int, username: str, role: str) -> None:
            job = app.get_job(job_id)
            if not job or not can_see_job(username, role, job.user, app.config):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            pdf_path = job.metadata.get("pdf_path")
            size_str = "0 KB"
            if pdf_path:
                try:
                    p = Path(pdf_path)
                    if p.exists():
                        size_bytes = p.stat().st_size
                        if size_bytes > 1024 * 1024:
                            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
                        else:
                            size_str = f"{size_bytes / 1024:.1f} KB"
                except Exception:
                    pass

            time_val = job.metadata.get("archived_at") or "Unknown"
            if "T" in time_val:
                time_val = time_val.replace("T", " ")[:19]

            state_badge = ""
            if job.state in {7, 8}:
                state_badge = '<span class="badge badge-red">Canceled</span>'
            elif job.state == 9:
                state_badge = '<span class="badge badge-green">Completed</span>'
            else:
                state_badge = '<span class="badge badge-blue">Pending</span>'

            owner_role = _get_user_role(job.user, app.config)
            role_badge = ""
            if owner_role == "administrator":
                role_badge = '<span class="badge-role role-admin">Admin</span>'
            elif owner_role == "staff":
                role_badge = '<span class="badge-role role-staff">Staff</span>'
            else:
                role_badge = '<span class="badge-role role-student">Student</span>'

            meta_rows = ""
            for k, v in sorted(job.metadata.items()):
                if k in {"pdf_path", "metadata_path"}:
                    continue
                meta_rows += f"""
                <div class="meta-item">
                    <span class="meta-key">{k}</span>
                    <span class="meta-val">{v}</span>
                </div>
                """

            embed_html = ""
            if pdf_path and Path(pdf_path).exists():
                embed_html = f"""
                <div style="margin-bottom: 2.5rem; text-align: center;">
                    <iframe src="/jobs/{job.job_id}/download?inline=true" width="100%" height="550px" style="border: 1px solid var(--border-color); border-radius: 12px; background: #0f172a; margin-bottom: 1rem;" id="pdf-viewer-frame"></iframe>
                    <a href="/jobs/{job.job_id}/download?inline=true" target="_blank" class="btn btn-blue" style="display: inline-block; padding: 0.8rem 1.8rem; font-size: 0.95rem; border-radius: 10px;" id="btn-detail-open">Open PDF in Browser Directly</a>
                </div>
                """

            actions_html = f'<a href="/jobs/{job.job_id}/download" class="btn btn-purple btn-large" id="btn-detail-dl">Download PDF Document</a> '
            actions_html += f'<a href="/jobs/{job.job_id}/metadata" class="btn btn-blue btn-large" id="btn-detail-meta">Download JSON Metadata</a> '
            if can_delete_job(username, role, job.user):
                actions_html += f'<a href="/jobs/{job.job_id}/delete" onclick="return confirm(\'Delete this job?\');" class="btn btn-red btn-large" id="btn-detail-del">Delete Print Job</a>'

            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Print Job Details - #{job.job_id}</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0b0f19;
            --bg-card: rgba(22, 28, 45, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --accent-blue: #3b82f6;
            --accent-purple: #8b5cf6;
            --accent-red: #ef4444;
            --accent-green: #10b981;
        }}
        body {{
            background: radial-gradient(circle at top, #1e1b4b, #0b0f19);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            padding: 3rem 1.5rem;
        }}
        .back-link {{
            display: inline-flex;
            align-items: center;
            color: var(--text-secondary);
            text-decoration: none;
            font-weight: 600;
            margin-bottom: 2rem;
            transition: color 0.2s;
        }}
        .back-link:hover {{
            color: var(--accent-blue);
        }}
        .glass-panel {{
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 2.5rem;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.4s ease-out forwards;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        h2 {{
            font-size: 2rem;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1rem;
        }}
        .grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1.5rem;
            margin-bottom: 2.5rem;
        }}
        .grid-item {{
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }}
        .grid-label {{
            color: var(--text-secondary);
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .grid-val {{
            font-size: 1.15rem;
            font-weight: 600;
        }}
        .badge {{
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
            display: inline-block;
            width: fit-content;
        }}
        .badge-green {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge-blue {{ background: rgba(59, 130, 246, 0.15); color: var(--accent-blue); border: 1px solid rgba(59, 130, 246, 0.3); }}
        .badge-red {{ background: rgba(239, 68, 68, 0.15); color: var(--accent-red); border: 1px solid rgba(239, 68, 68, 0.3); }}
        
        .badge-role {{
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            display: inline-block;
            width: fit-content;
        }}
        .role-admin {{ background: rgba(239, 68, 68, 0.12); color: var(--accent-red); }}
        .role-staff {{ background: rgba(139, 92, 246, 0.12); color: var(--accent-purple); }}
        .role-student {{ background: rgba(59, 130, 246, 0.12); color: var(--accent-blue); }}

        h3 {{
            font-size: 1.25rem;
            margin-bottom: 1rem;
            color: var(--text-secondary);
        }}
        .meta-container {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
            margin-bottom: 2.5rem;
            max-height: 300px;
            overflow-y: auto;
        }}
        .meta-item {{
            display: flex;
            justify-content: space-between;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            padding-bottom: 0.5rem;
        }}
        .meta-item:last-child {{
            border-bottom: none;
            padding-bottom: 0;
        }}
        .meta-key {{
            font-weight: 600;
            color: var(--text-secondary);
        }}
        .meta-val {{
            color: var(--text-primary);
            word-break: break-all;
            text-align: right;
            max-width: 60%;
        }}
        .btn-large {{
            display: block;
            width: 100%;
            padding: 1rem;
            text-align: center;
            border-radius: 12px;
            font-size: 1rem;
            font-weight: 600;
            box-sizing: border-box;
            margin-bottom: 1rem;
            text-decoration: none;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .btn-large:hover {{
            transform: translateY(-1px);
        }}
        .btn-blue {{ background: var(--accent-blue); color: #fff; }}
        .btn-blue:hover {{ box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }}
        .btn-purple {{ background: var(--accent-purple); color: #fff; }}
        .btn-purple:hover {{ box-shadow: 0 4px 12px rgba(139, 92, 246, 0.3); }}
        .btn-red {{ background: var(--accent-red); color: #fff; }}
        .btn-red:hover {{ box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3); }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="back-link" id="link-back-dashboard">
            <svg xmlns="http://www.w3.org/2000/svg" style="height: 1.25rem; width: 1.25rem; margin-right: 0.5rem;" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
            </svg>
            Back to Dashboard
        </a>

        <div class="glass-panel">
            <h2>Job Details #{job.job_id}</h2>
            {embed_html}
            
            <div class="grid">
                <div class="grid-item">
                    <span class="grid-label">Document Name</span>
                    <span class="grid-val">{job.name}</span>
                </div>
                <div class="grid-item">
                    <span class="grid-label">Owner</span>
                    <span class="grid-val" style="display: flex; align-items: center; gap: 0.5rem;">{job.user} {role_badge}</span>
                </div>
                <div class="grid-item">
                    <span class="grid-label">File Size</span>
                    <span class="grid-val">{size_str}</span>
                </div>
                <div class="grid-item">
                    <span class="grid-label">Archived At</span>
                    <span class="grid-val">{time_val}</span>
                </div>
                <div class="grid-item">
                    <span class="grid-label">Job Status</span>
                    {state_badge}
                </div>
            </div>

            <h3>Metadata Attributes</h3>
            <div class="meta-container">
                {meta_rows}
            </div>

            <div class="actions">
                {actions_html}
            </div>
        </div>
    </div>
</body>
</html>
"""
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _serve_download(self, job_id: int, username: str, role: str, file_type: str, inline: bool = False) -> None:
            job = app.get_job(job_id)
            if not job or not can_see_job(username, role, job.user, app.config):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if file_type == "pdf":
                path_str = job.metadata.get("pdf_path")
                mime_type = "application/pdf"
                filename = f"job-{job_id}.pdf"
            else:
                path_str = job.metadata.get("metadata_path")
                mime_type = "application/json"
                filename = f"job-{job_id}.json"

            if not path_str or not Path(path_str).exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            try:
                p = Path(path_str)
                content = p.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(content)))
                disposition = "inline" if inline else f'attachment; filename="{filename}"'
                self.send_header("Content-Disposition", disposition)
                self.end_headers()
                self.wfile.write(content)
            except Exception as exc:
                LOGGER.error("Failed to serve download: %s", exc)
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)

        def _handle_delete(self, job_id: int, username: str, role: str) -> None:
            job = app.get_job(job_id)
            if not job or not can_see_job(username, role, job.user, app.config):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if not can_delete_job(username, role, job.user):
                self.send_error(HTTPStatus.FORBIDDEN, "You do not have permission to delete this print job.")
                return

            pdf_path = job.metadata.get("pdf_path")
            metadata_path = job.metadata.get("metadata_path")

            with app._lock:
                if job_id in app._jobs:
                    del app._jobs[job_id]

            if pdf_path:
                try:
                    p = Path(pdf_path)
                    if p.exists():
                        p.unlink()
                except Exception as exc:
                    LOGGER.warning("Could not delete PDF file %s: %s", pdf_path, exc)

            if metadata_path:
                try:
                    p = Path(metadata_path)
                    if p.exists():
                        p.unlink()
                except Exception as exc:
                    LOGGER.warning("Could not delete metadata file %s: %s", metadata_path, exc)

            import urllib.parse
            msg = urllib.parse.quote(f"Job #{job_id} deleted successfully.")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/?msg={msg}")
            self.end_headers()

        def _handle_bulk_actions(self, username: str, role: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            post_data = self.rfile.read(length)
            from urllib.parse import parse_qs
            params = parse_qs(post_data.decode("utf-8"))
            action = params.get("bulk_action", [""])[0]
            job_id_strs = params.get("job_ids", [])
            
            job_ids = []
            for jid_str in job_id_strs:
                try:
                    job_ids.append(int(jid_str))
                except ValueError:
                    pass

            if not job_ids:
                import urllib.parse
                msg = urllib.parse.quote("No print jobs selected.")
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/?msg={msg}")
                self.end_headers()
                return

            if action == "zip":
                import tempfile
                import zipfile
                import os
                import shutil
                
                with tempfile.TemporaryFile() as tmp_file:
                    with zipfile.ZipFile(tmp_file, "w", zipfile.ZIP_DEFLATED) as zip_file:
                        for jid in job_ids:
                            job = app.get_job(jid)
                            if job and can_see_job(username, role, job.user, app.config):
                                pdf_path = job.metadata.get("pdf_path")
                                metadata_path = job.metadata.get("metadata_path")
                                
                                if pdf_path and Path(pdf_path).exists():
                                    try:
                                        rel_path = Path(pdf_path).relative_to(app.config.archive_root)
                                    except ValueError:
                                        rel_path = Path(pdf_path).name
                                    zip_file.write(pdf_path, arcname=str(rel_path))
                                
                                if metadata_path and Path(metadata_path).exists():
                                    try:
                                        rel_path = Path(metadata_path).relative_to(app.config.archive_root)
                                    except ValueError:
                                        rel_path = Path(metadata_path).name
                                    zip_file.write(metadata_path, arcname=str(rel_path))
                    
                    tmp_file.seek(0, os.SEEK_END)
                    file_size = tmp_file.tell()
                    tmp_file.seek(0)
                    
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(file_size))
                    self.send_header("Content-Disposition", 'attachment; filename="print-jobs-export.zip"')
                    self.end_headers()
                    
                    shutil.copyfileobj(tmp_file, self.wfile)
                return

            elif action == "delete":
                deleted_count = 0
                for jid in job_ids:
                    job = app.get_job(jid)
                    if job and can_see_job(username, role, job.user, app.config) and can_delete_job(username, role, job.user):
                        pdf_path = job.metadata.get("pdf_path")
                        metadata_path = job.metadata.get("metadata_path")

                        with app._lock:
                            if jid in app._jobs:
                                del app._jobs[jid]
                                deleted_count += 1

                        if pdf_path:
                            try:
                                p = Path(pdf_path)
                                if p.exists():
                                    p.unlink()
                            except Exception:
                                pass

                        if metadata_path:
                            try:
                                p = Path(metadata_path)
                                if p.exists():
                                    p.unlink()
                            except Exception:
                                pass

                import urllib.parse
                msg = urllib.parse.quote(f"Deleted {deleted_count} job(s) successfully.")
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/?msg={msg}")
                self.end_headers()
                return

            self.send_error(HTTPStatus.BAD_REQUEST)

        def _serve_config_editor(self) -> None:
            cfg = app.config
            timetable_rules = ""
            for idx, r in enumerate(cfg.timetable):
                users_str = ", ".join(r.users)
                days_str = ", ".join(r.days)
                timetable_rules += f"Rule #{idx+1}: Users={users_str}, Days={days_str}, Start={r.start}, End={r.end}, Folder={r.folder}\\n"

            if not timetable_rules:
                timetable_rules = "No active rules configured."

            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Configure Archive Printer</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0b0f19;
            --bg-card: rgba(22, 28, 45, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --accent-blue: #3b82f6;
            --accent-purple: #8b5cf6;
            --accent-red: #ef4444;
            --accent-green: #10b981;
        }}
        body {{
            background: radial-gradient(circle at top, #1e1b4b, #0b0f19);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            padding: 3rem 1.5rem;
        }}
        .back-link {{
            display: inline-flex;
            align-items: center;
            color: var(--text-secondary);
            text-decoration: none;
            font-weight: 600;
            margin-bottom: 2rem;
            transition: color 0.2s;
        }}
        .back-link:hover {{
            color: var(--accent-blue);
        }}
        .glass-panel {{
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 2.5rem;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
            animation: fadeIn 0.4s ease-out forwards;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        h2 {{
            font-size: 2rem;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1rem;
        }}
        .form-group {{
            margin-bottom: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }}
        label {{
            font-weight: 600;
            font-size: 0.95rem;
        }}
        input[type="text"], input[type="number"] {{
            padding: 0.75rem 1rem;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-family: inherit;
            font-size: 1rem;
        }}
        input:focus {{
            outline: none;
            border-color: var(--accent-blue);
        }}
        .checkbox-label {{
            flex-direction: row;
            align-items: center;
            gap: 0.75rem;
            cursor: pointer;
        }}
        .btn-large {{
            display: block;
            width: 100%;
            padding: 1rem;
            text-align: center;
            border-radius: 12px;
            font-size: 1rem;
            font-weight: 600;
            box-sizing: border-box;
            margin-top: 2rem;
            cursor: pointer;
            border: none;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .btn-large:hover {{
            transform: translateY(-1px);
        }}
        .btn-blue {{ background: var(--accent-blue); color: #fff; }}
        .btn-blue:hover {{ box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3); }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="back-link">
            <svg xmlns="http://www.w3.org/2000/svg" style="height: 1.25rem; width: 1.25rem; margin-right: 0.5rem;" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
            </svg>
            Back to Dashboard
        </a>

        <div class="glass-panel">
            <h2>Server Configuration</h2>
            <form method="POST" action="/config">
                <div class="form-group">
                    <label for="printer_name">Printer Name</label>
                    <input type="text" name="printer_name" id="printer_name" value="{cfg.printer_name}" required>
                </div>
                
                <div class="form-group">
                    <label for="timezone">System Timezone</label>
                    <input type="text" name="timezone" id="timezone" value="{cfg.timezone}" required>
                </div>

                <div class="form-group">
                    <label for="web_ui_domain">Web UI Domain Alias</label>
                    <input type="text" name="web_ui_domain" id="web_ui_domain" value="{cfg.web_ui_domain or ''}">
                </div>

                <div class="form-group">
                    <label for="low_disk_space_threshold_mb">Low Disk Space Warning Threshold (MB)</label>
                    <input type="number" name="low_disk_space_threshold_mb" id="low_disk_space_threshold_mb" value="{cfg.low_disk_space_threshold_mb}" required>
                </div>

                <div class="form-group checkbox-label" style="display: flex;">
                    <input type="checkbox" name="require_basic_auth" id="require_basic_auth" value="true" {"checked" if cfg.require_basic_auth else ""}>
                    <label for="require_basic_auth">Require Basic Authentication for Printing & Dashboard</label>
                </div>

                <div class="form-group" style="margin-top: 2rem;">
                    <label>Active Timetable Rules (Read Only)</label>
                    <pre style="background: rgba(0, 0, 0, 0.2); padding: 1rem; border-radius: 8px; font-family: monospace; font-size: 0.85rem; color: var(--text-secondary); margin: 0; white-space: pre-wrap;">{timetable_rules}</pre>
                </div>

                <button type="submit" class="btn-large btn-blue">Save & Apply Settings</button>
            </form>
        </div>
    </div>
</body>
</html>
"""
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _handle_config_save(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            post_data = self.rfile.read(length)
            from urllib.parse import parse_qs
            params = parse_qs(post_data.decode("utf-8"))

            printer_name = params.get("printer_name", [""])[0]
            timezone = params.get("timezone", [""])[0]
            web_ui_domain = params.get("web_ui_domain", [""])[0].strip() or None
            
            try:
                low_disk_space_threshold_mb = int(params.get("low_disk_space_threshold_mb", ["1000"])[0])
            except ValueError:
                low_disk_space_threshold_mb = 1000

            require_basic_auth = params.get("require_basic_auth", [""])[0] == "true"

            import json
            config_file = app.config.config_path
            
            data = {}
            if config_file.exists():
                try:
                    data = json.loads(config_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            data["printer_name"] = printer_name
            data["timezone"] = timezone
            data["web_ui_domain"] = web_ui_domain
            data["low_disk_space_threshold_mb"] = low_disk_space_threshold_mb
            data["require_basic_auth"] = require_basic_auth

            try:
                config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                from .config import AppConfig
                app.config = AppConfig.load(config_file)
                
                import urllib.parse
                msg = urllib.parse.quote("Configuration updated and applied successfully.")
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/?msg={msg}")
                self.end_headers()
            except Exception as exc:
                LOGGER.error("Failed to save config: %s", exc)
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to save configuration: {exc}")

        def do_HEAD(self) -> None:
            if self.path in {"/", "/healthz", "/ipp/print"}:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            host_header = self.headers.get("Host", "").split(":")[0]
            is_web_ui = False
            
            if app.config.web_ui_domain and host_header.lower() == app.config.web_ui_domain.lower():
                is_web_ui = True
            elif not app.config.web_ui_domain and (path == "/" or path.startswith("/jobs") or path == "/config"):
                is_web_ui = True

            if is_web_ui:
                auth_info = self._get_authenticated_user_and_role()
                if not auth_info:
                    self.send_response(HTTPStatus.UNAUTHORIZED)
                    self.send_header("WWW-Authenticate", 'Basic realm="Archive Printer Web UI"')
                    self.end_headers()
                    return
                username, role = auth_info

                if path == "/jobs/bulk":
                    self._handle_bulk_actions(username, role)
                    return
                elif path == "/config":
                    if role == "administrator":
                        self._handle_config_save()
                    else:
                        self.send_error(HTTPStatus.FORBIDDEN)
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
                return

            content_type = self.headers.get("Content-Type", "")
            if "application/ipp" not in content_type:
                self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/ipp")
                return

            auth_user = self._authenticated_user()
            if app.config.require_basic_auth and not auth_user:
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("WWW-Authenticate", 'Basic realm="Archive Printer"')
                self.end_headers()
                return

            request = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0:
                    self._send_ipp_error(Status.CLIENT_ERROR_BAD_REQUEST)
                    return
                if length > 2 * 1024 * 1024 * 1024:
                    self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request payload exceeds 2 GB limit")
                    return
                request = parse_request_stream(self.rfile, length)
            except Exception as exc:
                LOGGER.warning("bad IPP request: %s", exc)
                self._send_ipp_error(Status.CLIENT_ERROR_BAD_REQUEST)
                return

            try:
                metadata = {"client-address": self.client_address[0]}
                if auth_user:
                    metadata["auth-user"] = auth_user
                LOGGER.debug("IPP %s from %s attrs=%s", request.operation, self.client_address[0], request.attributes)

                try:
                    payload = self._handle_ipp(request, metadata)
                except UnsupportedDocumentFormat as exc:
                    LOGGER.info("rejected job: %s", exc)
                    payload = response(request, Status.CLIENT_ERROR_DOCUMENT_FORMAT_NOT_SUPPORTED)
                except Exception:
                    LOGGER.exception("failed to handle IPP request")
                    payload = response(request, Status.SERVER_ERROR_INTERNAL_ERROR)

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/ipp")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                if request and isinstance(request.document, str):
                    import os
                    if os.path.exists(request.document):
                        try:
                            os.remove(request.document)
                        except Exception:
                            pass

        def _handle_ipp(self, request: IppRequest, metadata: dict[str, Any]) -> bytes:
            operation = request.operation
            if operation == Operation.GET_PRINTER_ATTRIBUTES:
                req_attrs = request.attributes.get("requested-attributes")
                if req_attrs and isinstance(req_attrs, str):
                    req_attrs = [req_attrs]
                return response(request, Status.SUCCESSFUL_OK, printer_attribute_groups(app.config, self.printer_uri(), req_attrs))

            if operation == Operation.VALIDATE_JOB:
                status, unsupported = validate_job_attributes(request, app.config)
                groups = []
                if unsupported:
                    groups.append((GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported))
                req_attrs = request.attributes.get("requested-attributes")
                if req_attrs and isinstance(req_attrs, str):
                    req_attrs = [req_attrs]
                groups.extend(printer_attribute_groups(app.config, self.printer_uri(), req_attrs))
                return response(request, status, groups)

            if operation == Operation.GET_JOBS:
                which_jobs = request.attributes.get("which-jobs", "not-completed")
                if isinstance(which_jobs, list):
                    which_jobs = which_jobs[0]
                which_jobs = str(which_jobs).casefold()

                jobs = app.jobs()
                if which_jobs == "completed":
                    jobs = [j for j in jobs if j.state in {7, 8, 9}]
                else:  # default/not-completed
                    jobs = [j for j in jobs if j.state in {3, 4, 5, 6}]

                my_jobs = request.attributes.get("my-jobs", False)
                if isinstance(my_jobs, list):
                    my_jobs = my_jobs[0]
                my_jobs = bool(my_jobs)
                if my_jobs:
                    req_user = request.attributes.get("requesting-user-name")
                    if req_user:
                        req_user_str = str(req_user).casefold()
                        jobs = [j for j in jobs if str(j.user).casefold() == req_user_str]

                limit = request.attributes.get("limit")
                if limit:
                    if isinstance(limit, list):
                        limit = limit[0]
                    try:
                        limit_val = int(limit)
                        if limit_val > 0:
                            jobs = jobs[:limit_val]
                    except (ValueError, TypeError):
                        pass

                req_attrs = request.attributes.get("requested-attributes")
                if req_attrs and isinstance(req_attrs, str):
                    req_attrs = [req_attrs]

                return response(request, Status.SUCCESSFUL_OK, jobs_attribute_groups(jobs, req_attrs))

            if operation == Operation.CREATE_JOB:
                status, unsupported = validate_job_attributes(request, app.config)
                if status == Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED:
                    return response(request, status, [(GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported)])
                
                job = app.next_job(request, metadata)
                resp_groups = []
                if unsupported:
                    resp_groups.append((GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported))
                resp_groups.append(job_attributes(job))
                return response(request, status, resp_groups)

            if operation == Operation.SEND_DOCUMENT:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                
                status, unsupported = validate_job_attributes(request, app.config)
                if status == Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED:
                    return response(request, status, [(GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported)])

                combined = {**job.metadata, **request.attributes, **metadata}
                archived = app.store.store(request.document, combined)
                job.metadata["pdf_path"] = str(archived.pdf_path)
                job.metadata["metadata_path"] = str(archived.metadata_path)
                LOGGER.info("archived job %s to %s", job.job_id, archived.pdf_path)

                last_document = request.attributes.get("last-document", True)
                if isinstance(last_document, list):
                    last_document = last_document[0]
                if bool(last_document):
                    job.state = 9  # completed
                else:
                    job.state = 5  # processing

                resp_groups = []
                if unsupported:
                    resp_groups.append((GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported))
                resp_groups.append(job_attributes(job))
                return response(request, status, resp_groups)

            if operation == Operation.PRINT_JOB:
                status, unsupported = validate_job_attributes(request, app.config)
                if status == Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED:
                    return response(request, status, [(GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported)])

                job = app.next_job(request, metadata)
                job.state = 9  # completed immediately since it has the document
                archived = app.store.store(request.document, job.metadata)
                job.metadata["pdf_path"] = str(archived.pdf_path)
                job.metadata["metadata_path"] = str(archived.metadata_path)
                LOGGER.info("archived job %s to %s", job.job_id, archived.pdf_path)

                resp_groups = []
                if unsupported:
                    resp_groups.append((GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported))
                resp_groups.append(job_attributes(job))
                return response(request, status, resp_groups)

            if operation == Operation.GET_JOB_ATTRIBUTES:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                
                req_attrs = request.attributes.get("requested-attributes")
                if req_attrs and isinstance(req_attrs, str):
                    req_attrs = [req_attrs]
                return response(request, Status.SUCCESSFUL_OK, [job_attributes(job, req_attrs)])

            if operation == Operation.CANCEL_JOB:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                job.state = 7  # canceled
                return response(request, Status.SUCCESSFUL_OK, [job_attributes(job)])

            if operation == Operation.HOLD_JOB:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                job.state = 4  # pending-held
                return response(request, Status.SUCCESSFUL_OK, [job_attributes(job)])

            if operation == Operation.RELEASE_JOB:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                job.state = 9  # completed (since we complete immediately)
                return response(request, Status.SUCCESSFUL_OK, [job_attributes(job)])

            if operation == Operation.RESTART_JOB:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                job.state = 9  # completed (since we complete immediately)
                return response(request, Status.SUCCESSFUL_OK, [job_attributes(job)])

            if operation == Operation.PRINT_URI:
                status, unsupported = validate_job_attributes(request, app.config)
                if status == Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED:
                    return response(request, status, [(GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported)])

                job = app.next_job(request, metadata)
                job.state = 9  # completed immediately
                document_uri = request.attributes.get("document-uri")
                if not document_uri:
                    return response(request, Status.CLIENT_ERROR_BAD_REQUEST)
                try:
                    import urllib.request
                    with urllib.request.urlopen(str(document_uri), timeout=10) as resp:
                        content_len = resp.headers.get("Content-Length")
                        if content_len:
                            try:
                                if int(content_len) > 100 * 1024 * 1024:
                                    LOGGER.warning("document-uri %s Content-Length exceeds 100 MB", document_uri)
                                    return response(request, Status.CLIENT_ERROR_BAD_REQUEST)
                            except (ValueError, TypeError):
                                pass
                        document_bytes = resp.read(100 * 1024 * 1024 + 1)
                        if len(document_bytes) > 100 * 1024 * 1024:
                            LOGGER.warning("document-uri %s content size exceeds 100 MB", document_uri)
                            return response(request, Status.CLIENT_ERROR_BAD_REQUEST)
                except Exception as exc:
                    LOGGER.warning("failed to fetch document-uri %s: %s", document_uri, exc)
                    return response(request, Status.CLIENT_ERROR_BAD_REQUEST)

                archived = app.store.store(document_bytes, job.metadata)
                job.metadata["pdf_path"] = str(archived.pdf_path)
                job.metadata["metadata_path"] = str(archived.metadata_path)
                LOGGER.info("archived job %s from URI %s to %s", job.job_id, document_uri, archived.pdf_path)
                
                resp_groups = []
                if unsupported:
                    resp_groups.append((GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported))
                resp_groups.append(job_attributes(job))
                return response(request, status, resp_groups)

            if operation == Operation.SEND_URI:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                
                status, unsupported = validate_job_attributes(request, app.config)
                if status == Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED:
                    return response(request, status, [(GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported)])

                document_uri = request.attributes.get("document-uri")
                if not document_uri:
                    return response(request, Status.CLIENT_ERROR_BAD_REQUEST)
                try:
                    import urllib.request
                    with urllib.request.urlopen(str(document_uri), timeout=10) as resp:
                        content_len = resp.headers.get("Content-Length")
                        if content_len:
                            try:
                                if int(content_len) > 100 * 1024 * 1024:
                                    LOGGER.warning("document-uri %s Content-Length exceeds 100 MB", document_uri)
                                    return response(request, Status.CLIENT_ERROR_BAD_REQUEST)
                            except (ValueError, TypeError):
                                pass
                        document_bytes = resp.read(100 * 1024 * 1024 + 1)
                        if len(document_bytes) > 100 * 1024 * 1024:
                            LOGGER.warning("document-uri %s content size exceeds 100 MB", document_uri)
                            return response(request, Status.CLIENT_ERROR_BAD_REQUEST)
                except Exception as exc:
                    LOGGER.warning("failed to fetch document-uri %s: %s", document_uri, exc)
                    return response(request, Status.CLIENT_ERROR_BAD_REQUEST)

                combined = {**job.metadata, **request.attributes, **metadata}
                archived = app.store.store(document_bytes, combined)
                job.metadata["pdf_path"] = str(archived.pdf_path)
                job.metadata["metadata_path"] = str(archived.metadata_path)
                LOGGER.info("archived job %s from URI %s to %s", job.job_id, document_uri, archived.pdf_path)

                last_document = request.attributes.get("last-document", True)
                if isinstance(last_document, list):
                    last_document = last_document[0]
                if bool(last_document):
                    job.state = 9  # completed
                else:
                    job.state = 5  # processing

                resp_groups = []
                if unsupported:
                    resp_groups.append((GroupTag.UNSUPPORTED_ATTRIBUTES, unsupported))
                resp_groups.append(job_attributes(job))
                return response(request, status, resp_groups)

            if operation == Operation.GET_PRINTER_SUPPORTED_VALUES:
                req_attrs = request.attributes.get("requested-attributes")
                if not req_attrs:
                    return response(request, Status.SUCCESSFUL_OK, printer_attribute_groups(app.config, self.printer_uri()))
                if isinstance(req_attrs, str):
                    req_attrs = [req_attrs]
                all_attrs = printer_attribute_groups(app.config, self.printer_uri())[0][1]
                matching_attrs = []
                for name in req_attrs:
                    lookup_names = {name, f"{name}-supported"}
                    for tag, attr_name, val in all_attrs:
                        if attr_name in lookup_names:
                            matching_attrs.append((tag, attr_name, val))
                return response(request, Status.SUCCESSFUL_OK, [(GroupTag.PRINTER_ATTRIBUTES, matching_attrs)])

            if operation == Operation.CREATE_PRINTER_SUBSCRIPTIONS:
                sub = app.next_subscription(request)
                return response(request, Status.SUCCESSFUL_OK, [subscription_attributes(sub)])

            if operation == Operation.CREATE_JOB_SUBSCRIPTIONS:
                job_id = _safe_int(request.attributes.get("job-id"))
                if not job_id and "job-uri" in request.attributes:
                    import re
                    match = re.search(r"/jobs/(\d+)", str(request.attributes["job-uri"]))
                    if match:
                        job_id = int(match.group(1))
                job = app.get_job(job_id)
                if not job:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                sub = app.next_subscription(request, job_id=job.job_id)
                return response(request, Status.SUCCESSFUL_OK, [subscription_attributes(sub)])

            if operation == Operation.GET_SUBSCRIPTION_ATTRIBUTES:
                sub_id = _safe_int(request.attributes.get("notify-subscription-id"))
                sub = app.get_subscription(sub_id)
                if not sub:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                return response(request, Status.SUCCESSFUL_OK, [subscription_attributes(sub)])

            if operation == Operation.GET_SUBSCRIPTIONS:
                groups = [subscription_attributes(sub) for sub in app.subscriptions()]
                return response(request, Status.SUCCESSFUL_OK, groups)

            if operation == Operation.RENEW_SUBSCRIPTION:
                sub_id = _safe_int(request.attributes.get("notify-subscription-id"))
                sub = app.get_subscription(sub_id)
                if not sub:
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                lease_duration = _safe_int(request.attributes.get("notify-lease-duration"), sub.lease_duration)
                sub.lease_duration = lease_duration
                return response(request, Status.SUCCESSFUL_OK, [subscription_attributes(sub)])

            if operation == Operation.CANCEL_SUBSCRIPTION:
                sub_id = _safe_int(request.attributes.get("notify-subscription-id"))
                if not app.remove_subscription(sub_id):
                    return response(request, Status.CLIENT_ERROR_NOT_FOUND)
                return response(request, Status.SUCCESSFUL_OK)

            if operation == Operation.GET_NOTIFICATIONS:
                return response(request, Status.SUCCESSFUL_OK)

            return response(request, Status.SERVER_ERROR_OPERATION_NOT_SUPPORTED)

        def _authenticated_user(self) -> str | None:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return None
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8", errors="replace")
            except Exception:
                return None
            username, _, _password = decoded.partition(":")
            return username or None

        def printer_uri(self) -> str:
            host = self.headers.get("Host") or f"localhost:{app.config.port}"
            parsed = urlsplit(self.path)
            path = parsed.path if parsed.path == "/ipp/print" else "/ipp/print"
            scheme = "ipps" if app.config.enable_tls else "ipp"
            return f"{scheme}://{host}{path}"

        def _send_ipp_error(self, status: Status) -> None:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "application/ipp")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def printer_attribute_groups(config: AppConfig, printer_uri: str, requested_attributes: list[str] | None = None) -> list[tuple[GroupTag, list[tuple[int, str, Any]]]]:
    auth_supported = "basic" if config.require_basic_auth else "none"
    sec_supported = "tls" if config.enable_tls else "none"
    more_info_uri = printer_uri.replace("ipps://", "https://", 1).replace("ipp://", "http://", 1)

    all_attrs = [
        (ValueTag.URI, "printer-uri-supported", printer_uri),
        (ValueTag.NAME_WITHOUT_LANGUAGE, "printer-name", config.printer_name),
        (ValueTag.TEXT_WITHOUT_LANGUAGE, "printer-info", config.printer_name),
        (ValueTag.TEXT_WITHOUT_LANGUAGE, "printer-make-and-model", "Archive Printer PDF Sink"),
        (ValueTag.TEXT_WITHOUT_LANGUAGE, "printer-location", ""),
        (ValueTag.URI, "printer-more-info", more_info_uri),
        (ValueTag.NAME_WITHOUT_LANGUAGE, "printer-dns-sd-name", f"{config.printer_name}._ipp._tcp.local."),
        (ValueTag.TEXT_WITHOUT_LANGUAGE, "printer-device-id", "MFG:ArchivePrinter;MDL:PDF Sink;CMD:PDF;CLS:PRINTER;"),
        (ValueTag.ENUM, "printer-state", 3),
        (ValueTag.KEYWORD, "printer-state-reasons", "none"),
        (ValueTag.KEYWORD, "ipp-versions-supported", ["1.1", "2.0", "2.1"]),
        (ValueTag.ENUM, "operations-supported", [int(op) for op in Operation]),
        (ValueTag.CHARSET, "charset-supported", "utf-8"),
        (ValueTag.CHARSET, "charset-configured", "utf-8"),
        (ValueTag.NATURAL_LANGUAGE, "generated-natural-language-supported", "en"),
        (ValueTag.NATURAL_LANGUAGE, "natural-language-configured", "en"),
        (ValueTag.KEYWORD, "uri-authentication-supported", auth_supported),
        (ValueTag.KEYWORD, "uri-security-supported", sec_supported),
        (ValueTag.KEYWORD, "compression-supported", "none"),
        (ValueTag.MIME_MEDIA_TYPE, "document-format-supported", "application/pdf"),
        (ValueTag.MIME_MEDIA_TYPE, "document-format-default", "application/pdf"),
        (ValueTag.KEYWORD, "media-supported", SUPPORTED_MEDIA),
        (ValueTag.KEYWORD, "media-ready", SUPPORTED_MEDIA),
        (ValueTag.KEYWORD, "media-default", DEFAULT_MEDIA),
        (ValueTag.BOOLEAN, "color-supported", True),
        (ValueTag.BOOLEAN, "printer-is-accepting-jobs", True),
        (ValueTag.KEYWORD, "pdl-override-supported", "not-attempted"),
        (ValueTag.BOOLEAN, "multiple-document-jobs-supported", False),
        (ValueTag.INTEGER, "multiple-operation-time-out", 300),
    ]
    if not requested_attributes:
        return [(GroupTag.PRINTER_ATTRIBUTES, all_attrs)]

    reqs = [str(x).casefold() for x in requested_attributes]
    if "all" in reqs:
        return [(GroupTag.PRINTER_ATTRIBUTES, all_attrs)]

    filtered = []
    for tag, name, val in all_attrs:
        name_lower = name.casefold()
        if name_lower in reqs:
            filtered.append((tag, name, val))
        elif "printer-description" in reqs and name_lower in {
            "printer-name", "printer-info", "printer-make-and-model", "printer-location", "printer-more-info", 
            "printer-dns-sd-name", "printer-device-id", "printer-state", "printer-state-reasons", 
            "ipp-versions-supported", "operations-supported", "charset-supported", "charset-configured", 
            "generated-natural-language-supported", "natural-language-configured", "printer-is-accepting-jobs"
        }:
            filtered.append((tag, name, val))
        elif "job-template" in reqs and name_lower in {
            "media-supported", "media-ready", "media-default", "color-supported", "compression-supported", 
            "document-format-supported", "document-format-default", "pdl-override-supported", 
            "multiple-document-jobs-supported", "multiple-operation-time-out"
        }:
            filtered.append((tag, name, val))

    return [(GroupTag.PRINTER_ATTRIBUTES, filtered)]


def job_attributes(job: Job, requested_attributes: list[str] | None = None) -> tuple[GroupTag, list[tuple[int, str, Any]]]:
    all_attrs = [
        (ValueTag.INTEGER, "job-id", job.job_id),
        (ValueTag.URI, "job-uri", f"ipp://localhost:8631/jobs/{job.job_id}"),
        (ValueTag.NAME_WITHOUT_LANGUAGE, "job-name", job.name),
        (ValueTag.NAME_WITHOUT_LANGUAGE, "job-originating-user-name", job.user),
        (ValueTag.ENUM, "job-state", job.state),
    ]

    metadata_mappings = {
        "copies": ValueTag.INTEGER,
        "media": ValueTag.KEYWORD,
        "sides": ValueTag.KEYWORD,
        "print-quality": ValueTag.ENUM,
        "printer-resolution": ValueTag.RESOLUTION,
        "orientation-requested": ValueTag.ENUM,
        "document-format": ValueTag.MIME_MEDIA_TYPE,
        "requesting-user-name": ValueTag.NAME_WITHOUT_LANGUAGE,
        "job-name": ValueTag.NAME_WITHOUT_LANGUAGE,
    }

    for key, tag in metadata_mappings.items():
        if key in job.metadata:
            if key not in {"job-name", "requesting-user-name"}:
                all_attrs.append((tag, key, job.metadata[key]))

    if not requested_attributes:
        return (GroupTag.JOB_ATTRIBUTES, all_attrs)

    reqs = [str(x).casefold() for x in requested_attributes]
    if "all" in reqs or "job-description" in reqs or "job-template" in reqs:
        return (GroupTag.JOB_ATTRIBUTES, all_attrs)

    filtered = []
    for tag, name, val in all_attrs:
        if name.casefold() in reqs:
            filtered.append((tag, name, val))

    if not filtered:
        filtered = [(ValueTag.INTEGER, "job-id", job.job_id)]
    return (GroupTag.JOB_ATTRIBUTES, filtered)


def jobs_attribute_groups(jobs: list[Job], requested_attributes: list[str] | None = None) -> list[tuple[GroupTag, list[tuple[int, str, Any]]]]:
    return [job_attributes(job, requested_attributes) for job in jobs]


def validate_job_attributes(request: IppRequest, config: AppConfig) -> tuple[Status, list[tuple[int, str, Any]]]:
    unsupported = []

    doc_format = request.attributes.get("document-format")
    if doc_format:
        if isinstance(doc_format, list):
            doc_format = doc_format[0]
        doc_format_str = str(doc_format).casefold()
        if not config.pdf_converter_command and doc_format_str != "application/pdf":
            unsupported.append((ValueTag.MIME_MEDIA_TYPE, "document-format", doc_format))

    media = request.attributes.get("media")
    if media:
        if isinstance(media, list):
            for m in media:
                if str(m) not in SUPPORTED_MEDIA:
                    unsupported.append((ValueTag.KEYWORD, "media", m))
        else:
            if str(media) not in SUPPORTED_MEDIA:
                unsupported.append((ValueTag.KEYWORD, "media", media))

    copies = request.attributes.get("copies")
    if copies is not None:
        if isinstance(copies, list):
            copies = copies[0]
        try:
            c_val = int(copies)
            if c_val <= 0:
                unsupported.append((ValueTag.INTEGER, "copies", copies))
        except (ValueError, TypeError):
            unsupported.append((ValueTag.INTEGER, "copies", copies))

    if unsupported:
        fidelity = request.attributes.get("ipp-attribute-fidelity", False)
        if isinstance(fidelity, list):
            fidelity = fidelity[0]
        fidelity = bool(fidelity)
        if fidelity:
            return Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED, unsupported
        else:
            return Status.SUCCESSFUL_OK_CONFLICTING_ATTRIBUTES, unsupported

    return Status.SUCCESSFUL_OK, []


def subscription_attributes(sub: Subscription) -> tuple[GroupTag, list[tuple[int, str, Any]]]:
    return (
        GroupTag.SUBSCRIPTION_ATTRIBUTES,
        [
            (ValueTag.INTEGER, "notify-subscription-id", sub.subscription_id),
            (ValueTag.URI, "notify-recipient-uri", sub.recipient_uri),
            (ValueTag.KEYWORD, "notify-events", sub.events),
            (ValueTag.INTEGER, "notify-lease-duration", sub.lease_duration),
            (ValueTag.NAME_WITHOUT_LANGUAGE, "notify-subscriber-user-name", sub.user),
        ],
    )


def setup_letsencrypt(config: AppConfig) -> tuple[str, str]:
    if not config.letsencrypt_domain:
        raise ValueError("letsencrypt_domain must be configured to use Let's Encrypt")
    if not config.letsencrypt_email:
        raise ValueError("letsencrypt_email must be configured to use Let's Encrypt")

    from pathlib import Path
    import os
    if os.name == "nt":
        certbot_dir = Path("C:/Certbot/live") / config.letsencrypt_domain
    else:
        certbot_dir = Path("/etc/letsencrypt/live") / config.letsencrypt_domain

    cert_path = certbot_dir / "fullchain.pem"
    key_path = certbot_dir / "privkey.pem"

    if not cert_path.exists() or not key_path.exists():
        LOGGER.info("Let's Encrypt certificate not found at %s. Attempting to obtain via DNS authentication...", cert_path)
        
        cmd = [
            "certbot", "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email", config.letsencrypt_email,
            "-d", config.letsencrypt_domain
        ]

        provider = config.letsencrypt_dns_provider.lower().strip()
        if provider == "manual":
            cmd.extend(["--manual", "--preferred-challenges", "dns", "--manual-public-ip-logging-ok"])
        elif provider == "cloudflare":
            cmd.append("--dns-cloudflare")
            if config.letsencrypt_dns_credentials_file:
                cmd.extend(["--dns-cloudflare-credentials", config.letsencrypt_dns_credentials_file])
        elif provider == "route53":
            cmd.append("--dns-route53")
        elif provider == "digitalocean":
            cmd.append("--dns-digitalocean")
            if config.letsencrypt_dns_credentials_file:
                cmd.extend(["--dns-digitalocean-credentials", config.letsencrypt_dns_credentials_file])
        elif provider == "google":
            cmd.append("--dns-google")
            if config.letsencrypt_dns_credentials_file:
                cmd.extend(["--dns-google-credentials", config.letsencrypt_dns_credentials_file])
        else:
            cmd.extend([
                "--manual", "--preferred-challenges", "dns",
                "--manual-auth-hook", provider,
                "--manual-public-ip-logging-ok"
            ])

        LOGGER.info("Running Certbot command: %s", " ".join(cmd))
        try:
            import subprocess
            subprocess.run(cmd, check=True)
            LOGGER.info("Certbot completed successfully. Certificate obtained!")
        except Exception as exc:
            LOGGER.error("Certbot challenge failed: %s", exc)
            raise exc

    return str(cert_path), str(key_path)


def run(config: AppConfig) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.archive_root.mkdir(parents=True, exist_ok=True)
    app = ArchivePrinterApp(config)
    app.load_persisted_jobs()
    app.start_disk_monitor()
    httpd = ThreadingHTTPServer((config.bind_host, config.port), make_handler(app))
    
    if config.enable_tls:
        from pathlib import Path
        if config.use_letsencrypt:
            try:
                cert_file, key_file = setup_letsencrypt(config)
            except Exception as exc:
                LOGGER.error("Failed to set up Let's Encrypt TLS: %s. Falling back to self-signed cert.", exc)
                cert_file, key_file = None, None
        else:
            cert_file = config.ssl_certfile
            key_file = config.ssl_keyfile

        if not cert_file or not key_file:
            generated_dir = config.archive_root / ".ssl"
            generated_dir.mkdir(parents=True, exist_ok=True)
            cert_file = str(generated_dir / "cert.pem")
            key_file = str(generated_dir / "key.pem")
            if not Path(cert_file).exists() or not Path(key_file).exists():
                try:
                    import subprocess
                    cmd = [
                        "openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-keyout", key_file, "-out", cert_file,
                        "-days", "365", "-nodes",
                        "-subj", "/CN=archive-printer"
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    LOGGER.info("auto-generated self-signed TLS certificate at %s", cert_file)
                except Exception as exc:
                    LOGGER.error("Failed to auto-generate self-signed TLS cert: %s. Please install 'openssl' or configure ssl_certfile/ssl_keyfile manually.", exc)
                    raise exc

        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        LOGGER.info("TLS/HTTPS enabled")

    mdns = MdnsPublisher(config)
    LOGGER.info("listening on %s:%s, archiving to %s", config.bind_host, config.port, config.archive_root)
    mdns.start()
    try:
        httpd.serve_forever()
    finally:
        mdns.stop()
        app.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Archive Printer IPP server")
    parser.add_argument("--config", help="Path to config JSON")
    args = parser.parse_args()
    run(AppConfig.load(args.config))


if __name__ == "__main__":
    main()
