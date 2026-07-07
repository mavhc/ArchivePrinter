import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from archive_printer.config import AppConfig
from archive_printer.ipp import GroupTag, IppRequest, Operation, Status, ValueTag, parse_request, response
from archive_printer.server import ArchivePrinterApp, Job, Subscription, make_handler


class MockHandler:
    def __init__(self, app):
        self.app = app
        self.path = "/ipp/print"
        self.headers = {"Host": "localhost:8631"}

    def printer_uri(self):
        return "ipp://localhost:8631/ipp/print"


class ExtendedOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            archive_root=Path(self.tmp_dir.name),
            timezone=ZoneInfo("UTC"),
        )
        self.app = ArchivePrinterApp(self.config)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_get_job_attributes(self):
        # 1. Create a job first
        req_create = IppRequest(2, 0, Operation.CREATE_JOB, 1, {"requesting-user-name": "TestUser"}, b"")
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_create, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        job_id = res.attributes["job-id"]

        # 2. Get Job Attributes
        req_get = IppRequest(2, 0, Operation.GET_JOB_ATTRIBUTES, 2, {"job-id": job_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_get, {"client-address": "127.0.0.1"})
        res_get = parse_request(res_bytes)
        
        self.assertEqual(res_get.operation_id, Status.SUCCESSFUL_OK)
        self.assertEqual(res_get.attributes["job-id"], job_id)
        self.assertEqual(res_get.attributes["job-name"], f"Job {job_id}")

        # 3. Test not found
        req_not_found = IppRequest(2, 0, Operation.GET_JOB_ATTRIBUTES, 3, {"job-id": 9999}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_not_found, {"client-address": "127.0.0.1"})
        res_nf = parse_request(res_bytes)
        self.assertEqual(res_nf.operation_id, Status.CLIENT_ERROR_NOT_FOUND)

    def test_cancel_job(self):
        # Create job
        req_create = IppRequest(2, 0, Operation.CREATE_JOB, 1, {"requesting-user-name": "TestUser"}, b"")
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_create, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        job_id = res.attributes["job-id"]

        # Cancel job
        req_cancel = IppRequest(2, 0, Operation.CANCEL_JOB, 2, {"job-id": job_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_cancel, {"client-address": "127.0.0.1"})
        res_cancel = parse_request(res_bytes)
        
        self.assertEqual(res_cancel.operation_id, Status.SUCCESSFUL_OK)
        self.assertEqual(res_cancel.attributes["job-state"], 7)  # canceled

    def test_hold_and_release_job(self):
        # Create job
        req_create = IppRequest(2, 0, Operation.CREATE_JOB, 1, {"requesting-user-name": "TestUser"}, b"")
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_create, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        job_id = res.attributes["job-id"]

        # Hold job
        req_hold = IppRequest(2, 0, Operation.HOLD_JOB, 2, {"job-id": job_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_hold, {"client-address": "127.0.0.1"})
        res_hold = parse_request(res_bytes)
        self.assertEqual(res_hold.attributes["job-state"], 4)  # pending-held

        # Release job
        req_release = IppRequest(2, 0, Operation.RELEASE_JOB, 3, {"job-id": job_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_release, {"client-address": "127.0.0.1"})
        res_release = parse_request(res_bytes)
        self.assertEqual(res_release.attributes["job-state"], 9)  # completed

    def test_restart_job(self):
        # Create job
        req_create = IppRequest(2, 0, Operation.CREATE_JOB, 1, {"requesting-user-name": "TestUser"}, b"")
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_create, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        job_id = res.attributes["job-id"]

        # Restart job
        req_restart = IppRequest(2, 0, Operation.RESTART_JOB, 2, {"job-id": job_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_restart, {"client-address": "127.0.0.1"})
        res_restart = parse_request(res_bytes)
        self.assertEqual(res_restart.attributes["job-state"], 9)  # completed

    @patch("urllib.request.urlopen")
    def test_print_uri(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"%PDF-1.7\n%%EOF\n"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Print URI
        req_print = IppRequest(2, 0, Operation.PRINT_URI, 1, {"document-uri": "http://example.com/doc.pdf"}, b"")
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_print, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        
        self.assertEqual(res.operation_id, Status.SUCCESSFUL_OK)
        self.assertEqual(res.attributes["job-state"], 9)
        mock_urlopen.assert_called_once_with("http://example.com/doc.pdf", timeout=10)

    @patch("urllib.request.urlopen")
    def test_send_uri(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"%PDF-1.7\n%%EOF\n"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Create job
        req_create = IppRequest(2, 0, Operation.CREATE_JOB, 1, {"requesting-user-name": "TestUser"}, b"")
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_create, {"client-address": "127.0.0.1"})
        res_c = parse_request(res_bytes)
        job_id = res_c.attributes["job-id"]

        # Send URI
        req_send = IppRequest(2, 0, Operation.SEND_URI, 2, {"job-id": job_id, "document-uri": "http://example.com/doc.pdf"}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_send, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)

        self.assertEqual(res.operation_id, Status.SUCCESSFUL_OK)
        mock_urlopen.assert_called_once_with("http://example.com/doc.pdf", timeout=10)

    def test_get_printer_supported_values(self):
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}

        # All attributes
        req_all = IppRequest(2, 0, Operation.GET_PRINTER_SUPPORTED_VALUES, 1, {}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_all, {"client-address": "127.0.0.1"})
        res_all = parse_request(res_bytes)
        self.assertEqual(res_all.operation_id, Status.SUCCESSFUL_OK)
        self.assertIn("media-supported", res_all.attributes)

        # Specific attributes
        req_spec = IppRequest(2, 0, Operation.GET_PRINTER_SUPPORTED_VALUES, 2, {"requested-attributes": "media"}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_spec, {"client-address": "127.0.0.1"})
        res_spec = parse_request(res_bytes)
        self.assertEqual(res_spec.operation_id, Status.SUCCESSFUL_OK)
        self.assertIn("media-supported", res_spec.attributes)
        self.assertNotIn("printer-name", res_spec.attributes)

    def test_subscriptions_flow(self):
        HandlerClass = make_handler(self.app)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}

        # 1. Create printer subscription
        req_sub = IppRequest(
            2, 0, Operation.CREATE_PRINTER_SUBSCRIPTIONS, 1,
            {"notify-recipient-uri": "ipp://localhost/notify", "notify-lease-duration": 60}, b""
        )
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_sub, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)
        self.assertEqual(res.operation_id, Status.SUCCESSFUL_OK)
        sub_id = res.attributes["notify-subscription-id"]
        self.assertEqual(res.attributes["notify-lease-duration"], 60)

        # 2. Get subscription attributes
        req_get = IppRequest(2, 0, Operation.GET_SUBSCRIPTION_ATTRIBUTES, 2, {"notify-subscription-id": sub_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_get, {"client-address": "127.0.0.1"})
        res_get = parse_request(res_bytes)
        self.assertEqual(res_get.operation_id, Status.SUCCESSFUL_OK)
        self.assertEqual(res_get.attributes["notify-subscription-id"], sub_id)

        # 3. Renew subscription
        req_renew = IppRequest(
            2, 0, Operation.RENEW_SUBSCRIPTION, 3,
            {"notify-subscription-id": sub_id, "notify-lease-duration": 120}, b""
        )
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_renew, {"client-address": "127.0.0.1"})
        res_renew = parse_request(res_bytes)
        self.assertEqual(res_renew.attributes["notify-lease-duration"], 120)

        # 4. Get subscriptions list
        req_list = IppRequest(2, 0, Operation.GET_SUBSCRIPTIONS, 4, {}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_list, {"client-address": "127.0.0.1"})
        res_list = parse_request(res_bytes)
        self.assertEqual(res_list.operation_id, Status.SUCCESSFUL_OK)

        # 5. Get notifications
        req_notif = IppRequest(2, 0, Operation.GET_NOTIFICATIONS, 5, {}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_notif, {"client-address": "127.0.0.1"})
        res_notif = parse_request(res_bytes)
        self.assertEqual(res_notif.operation_id, Status.SUCCESSFUL_OK)

        # 6. Cancel subscription
        req_cancel = IppRequest(2, 0, Operation.CANCEL_SUBSCRIPTION, 6, {"notify-subscription-id": sub_id}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_cancel, {"client-address": "127.0.0.1"})
        res_cancel = parse_request(res_bytes)
        self.assertEqual(res_cancel.operation_id, Status.SUCCESSFUL_OK)

        # 7. Verify it is canceled (getting attributes returns NOT FOUND)
        res_bytes = HandlerClass._handle_ipp(handler_instance, req_get, {"client-address": "127.0.0.1"})
        res_get_nf = parse_request(res_bytes)
        self.assertEqual(res_get_nf.operation_id, Status.CLIENT_ERROR_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
