import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from archive_printer.config import AppConfig
from archive_printer.ipp import GroupTag, IppRequest, Operation, parse_request
from archive_printer.server import ArchivePrinterApp, Job, make_handler, job_attributes


class Section5FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            archive_root=Path(self.tmp_dir.name),
            timezone=ZoneInfo("UTC"),
        )
        self.app = ArchivePrinterApp(self.config)
        self.HandlerClass = make_handler(self.app)
        self.handler_instance = self.HandlerClass.__new__(self.HandlerClass)
        self.handler_instance.path = "/ipp/print"
        self.handler_instance.headers = {"Host": "localhost:8631"}

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_job_template_attributes_reflection(self):
        # Create a job with template attributes in the request
        req = IppRequest(2, 0, Operation.CREATE_JOB, 1, {
            "requesting-user-name": "Alice",
            "job-name": "TemplateDoc",
            "copies": 5,
            "media": "iso_a4_210x297mm",
            "sides": "two-sided-long-edge"
        }, b"")

        res_bytes = self.HandlerClass._handle_ipp(self.handler_instance, req, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        job_id = res.attributes["job-id"]

        # 1. Verify they are saved and returned on GET_JOB_ATTRIBUTES (default all)
        req_get = IppRequest(2, 0, Operation.GET_JOB_ATTRIBUTES, 2, {"job-id": job_id}, b"")
        res_get = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_get, {"client-address": "127.0.0.1"}))
        
        self.assertEqual(res_get.attributes["copies"], 5)
        self.assertEqual(res_get.attributes["media"], "iso_a4_210x297mm")
        self.assertEqual(res_get.attributes["sides"], "two-sided-long-edge")

        # 2. Verify we can request a specific template attribute
        req_get_spec = IppRequest(2, 0, Operation.GET_JOB_ATTRIBUTES, 3, {
            "job-id": job_id,
            "requested-attributes": ["copies"]
        }, b"")
        res_get_spec = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_get_spec, {"client-address": "127.0.0.1"}))
        self.assertEqual(res_get_spec.attributes["copies"], 5)
        self.assertNotIn("media", res_get_spec.attributes)

    def test_queue_pruning(self):
        # Populate app with 1005 jobs (with states as completed)
        for i in range(1, 1006):
            job = Job(
                job_id=i,
                name=f"Job {i}",
                user="Alice",
                state=9,
                metadata={}
            )
            self.app._jobs[i] = job

        self.app._next_job_id = 1006

        # Trigger pruning
        self.app._prune_jobs()

        # Verify it pruned down to 1000
        self.assertEqual(len(self.app._jobs), 1000)

        # Verify oldest jobs (1 to 5) are removed
        for i in range(1, 6):
            self.assertNotIn(i, self.app._jobs)
        
        # Verify newer jobs are kept
        self.assertIn(6, self.app._jobs)
        self.assertIn(1005, self.app._jobs)

    def test_job_persistence(self):
        # 1. Manually write a sidecar JSON representing an archived job
        user_dir = Path(self.tmp_dir.name) / "Alice" / "2026-07-07"
        user_dir.mkdir(parents=True, exist_ok=True)
        
        metadata = {
            "job-id": 42,
            "job-name": "PersistedDoc",
            "requesting-user-name": "Alice",
            "copies": 2
        }
        
        job_data = {
            "archived_at": "2026-07-07T09:10:30Z",
            "user": "Alice",
            "document_name": "PersistedDoc",
            "pdf_file": "PersistedDoc-timestamp.pdf",
            "source": metadata
        }
        
        json_file = user_dir / "PersistedDoc-timestamp.json"
        json_file.write_text(json.dumps(job_data), encoding="utf-8")

        # 2. Run load_persisted_jobs
        self.app.load_persisted_jobs()

        # 3. Verify job is restored in queue
        self.assertIn(42, self.app._jobs)
        restored = self.app._jobs[42]
        self.assertEqual(restored.name, "PersistedDoc")
        self.assertEqual(restored.user, "Alice")
        self.assertEqual(restored.state, 9)  # completed
        self.assertEqual(restored.metadata["copies"], 2)

    def test_timezone_fallback(self):
        # Setting invalid timezone should fall back to UTC instead of crashing
        import os
        import json
        
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text(json.dumps({
                "timezone": "Invalid/Timezone_Name",
                "archive_root": tmp
            }))
            
            config = AppConfig.load(config_file)
            self.assertEqual(config.timezone.key, "UTC")

    def test_subscription_pruning(self):
        # Create a subscription with very short lease (1 second)
        import time
        req = IppRequest(2, 0, Operation.CREATE_PRINTER_SUBSCRIPTIONS, 1, {
            "notify-recipient-uri": "ipp://localhost/notify",
            "notify-lease-duration": 1,
            "requesting-user-name": "Alice"
        }, b"")
        
        sub = self.app.next_subscription(req)
        sub_id = sub.subscription_id
        
        # Verify it exists
        self.assertIn(sub_id, self.app._subscriptions)
        self.assertEqual(len(self.app.subscriptions()), 1)
        
        # Wait for lease to expire (1.1s)
        time.sleep(1.1)
        
        # Retrieve subscriptions - it should trigger pruning and remove it
        self.assertEqual(len(self.app.subscriptions()), 0)
        self.assertIsNone(self.app.get_subscription(sub_id))

    def test_invalid_job_id_types_dont_crash_server(self):
        # 1. Test string job-id
        req_str = IppRequest(2, 0, Operation.GET_JOB_ATTRIBUTES, 1, {"job-id": "abc"}, b"")
        res_bytes = self.HandlerClass._handle_ipp(self.handler_instance, req_str, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        # Should return NOT_FOUND (0x0406) instead of throwing 500 error
        self.assertEqual(res.operation_id, 0x0406)

        # 2. Test list job-id
        req_list = IppRequest(2, 0, Operation.GET_JOB_ATTRIBUTES, 2, {"job-id": ["123"]}, b"")
        res_bytes2 = self.HandlerClass._handle_ipp(self.handler_instance, req_list, {"client-address": "127.0.0.1"})
        res2 = parse_request(res_bytes2)
        # Should return NOT_FOUND (0x0406) instead of throwing 500 error
        self.assertEqual(res2.operation_id, 0x0406)

    def test_content_length_bounds(self):
        from unittest.mock import MagicMock
        
        handler = self.HandlerClass.__new__(self.HandlerClass)
        handler.headers = {"Content-Type": "application/ipp", "Content-Length": "3000000000"}
        handler.rfile = MagicMock()
        handler.send_error = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)

        handler.do_POST()
        handler.send_error.assert_called_once_with(413, "request payload exceeds 2 GB limit")

        handler2 = self.HandlerClass.__new__(self.HandlerClass)
        handler2.headers = {"Content-Type": "application/ipp", "Content-Length": "-10"}
        handler2.rfile = MagicMock()
        handler2.send_error = MagicMock()
        handler2.send_response = MagicMock()
        handler2.send_header = MagicMock()
        handler2.end_headers = MagicMock()
        handler2.wfile = MagicMock()
        handler2._send_ipp_error = MagicMock()
        handler2.client_address = ("127.0.0.1", 12345)

        handler2.do_POST()
        handler2._send_ipp_error.assert_called_once()

    def test_document_uri_download_limit(self):
        from unittest.mock import patch, MagicMock
        
        # 1. Mock Content-Length header check
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": "200000000"}
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            
            req = IppRequest(2, 0, Operation.PRINT_URI, 1, {
                "document-uri": "http://example.com/huge.pdf"
            }, b"")
            res_bytes = self.HandlerClass._handle_ipp(self.handler_instance, req, {"client-address": "127.0.0.1"})
            res = parse_request(res_bytes)
            self.assertEqual(res.operation_id, 0x0400) # CLIENT_ERROR_BAD_REQUEST

        # 2. Mock payload size check on read
        mock_resp2 = MagicMock()
        mock_resp2.headers = {}
        mock_resp2.read.return_value = b"a" * (100 * 1024 * 1024 + 1)
        
        with patch("urllib.request.urlopen") as mock_urlopen2:
            mock_urlopen2.return_value.__enter__.return_value = mock_resp2
            
            res_bytes2 = self.HandlerClass._handle_ipp(self.handler_instance, req, {"client-address": "127.0.0.1"})
            res2 = parse_request(res_bytes2)
            self.assertEqual(res2.operation_id, 0x0400) # CLIENT_ERROR_BAD_REQUEST

    def test_disk_space_monitor_and_prediction(self):
        from unittest.mock import patch, MagicMock
        from datetime import date, timedelta
        
        # Set config threshold to 4800 MB so 4500 MB is "low space"
        self.app.config = AppConfig(
            archive_root=self.app.config.archive_root,
            timezone=self.app.config.timezone,
            low_disk_space_threshold_mb=4800
        )
        archive_root = self.app.config.archive_root
        archive_root.mkdir(parents=True, exist_ok=True)
        
        # Pre-populate history file: 5 days ago, 6000 MB free
        history_file = archive_root / "disk_space_history.json"
        day_old = (date.today() - timedelta(days=5)).isoformat()
        history_data = [{"date": day_old, "free_mb": 6000.0}]
        history_file.write_text(json.dumps(history_data), encoding="utf-8")

        # Mock shutil.disk_usage to return 4500 MB free (low space threshold)
        # 1500 MB consumption in 5 days -> 300 MB/day rate -> 4500 MB / 300 MB/day = 15 days remaining.
        with patch("shutil.disk_usage") as mock_disk_usage, \
             patch("archive_printer.server.LOGGER") as mock_logger:
            
            mock_disk_usage.return_value = MagicMock(free=4500 * 1024 * 1024, total=10000 * 1024 * 1024)
            
            self.app.check_disk_space()
            
            # Format and check logger.info calls
            info_calls = [call[0][0] % call[0][1:] for call in mock_logger.info.call_args_list]
            self.assertTrue(any("Disk Space Monitor: 4.39 GB free" in msg for msg in info_calls))
            self.assertTrue(any("approximately 15.0 days of space remain" in msg for msg in info_calls))
            
            # Format and check logger.warning calls
            warn_calls = [call[0][0] % call[0][1:] for call in mock_logger.warning.call_args_list]
            self.assertTrue(any("WARNING: Disk space is low!" in msg for msg in warn_calls))
            self.assertTrue(any("Prediction: 15.0 days left" in msg for msg in warn_calls))


    def test_web_ui_and_permissions(self):
        from unittest.mock import MagicMock
        from archive_printer.server import can_see_job, can_delete_job
        
        # 1. Define custom users and roles configuration
        self.app.config = AppConfig(
            archive_root=self.app.config.archive_root,
            timezone=self.app.config.timezone,
            web_ui_domain="admin-printer.local",
            users={
                "admin": {"password": "adminpass", "role": "administrator"},
                "teacher1": {"password": "teachpass", "role": "staff"},
                "student1": {"password": "stud1pass", "role": "student"},
                "student2": {"password": "stud2pass", "role": "student"},
            }
        )

        # Pre-populate some print jobs
        job1 = Job(1, "Homework 1", "student1", 9, {"pdf_path": "job1.pdf"})
        job2 = Job(2, "Homework 2", "student2", 9, {"pdf_path": "job2.pdf"})
        job3 = Job(3, "Exam 1", "teacher1", 9, {"pdf_path": "job3.pdf"})
        job4 = Job(4, "System Log", "admin", 9, {"pdf_path": "job4.pdf"})
        
        self.app._jobs = {1: job1, 2: job2, 3: job3, 4: job4}

        # 2. Test domain alias check on restricted path
        handler = self.HandlerClass.__new__(self.HandlerClass)
        handler.headers = {"Host": "print-ip:8631"}
        handler.path = "/jobs"
        handler.send_error = MagicMock()
        handler.do_GET()
        handler.send_error.assert_called_once_with(404)

        # 3. Test Authentication and Role visibility mapping
        # Admin can see everything
        self.assertTrue(can_see_job("admin", "administrator", "student1", self.app.config))
        self.assertTrue(can_see_job("admin", "administrator", "teacher1", self.app.config))
        
        # Staff (teacher1) can see students (student1, student2) and themselves, but not admin
        self.assertTrue(can_see_job("teacher1", "staff", "student1", self.app.config))
        self.assertTrue(can_see_job("teacher1", "staff", "teacher1", self.app.config))
        self.assertFalse(can_see_job("teacher1", "staff", "admin", self.app.config))

        # Student (student1) can only see student1 jobs
        self.assertTrue(can_see_job("student1", "student", "student1", self.app.config))
        self.assertFalse(can_see_job("student1", "student", "student2", self.app.config))
        self.assertFalse(can_see_job("student1", "student", "teacher1", self.app.config))

        # 4. Check delete permissions
        # Admin can delete anything
        self.assertTrue(can_delete_job("admin", "administrator", "student1"))
        # Staff can delete their own jobs, but not students'
        self.assertTrue(can_delete_job("teacher1", "staff", "teacher1"))
        self.assertFalse(can_delete_job("teacher1", "staff", "student1"))
        # Students cannot delete anything (even their own)
        self.assertFalse(can_delete_job("student1", "student", "student1"))


if __name__ == "__main__":
    unittest.main()
