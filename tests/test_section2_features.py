import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from archive_printer.config import AppConfig
from archive_printer.ipp import GroupTag, IppRequest, Operation, Status, ValueTag, parse_request
from archive_printer.server import ArchivePrinterApp, Job, make_handler


class Section2FeaturesTests(unittest.TestCase):
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

    def test_get_jobs_filtering(self):
        # Create Job 1: Alice, completed (via print job)
        req1 = IppRequest(2, 0, Operation.PRINT_JOB, 1, {
            "requesting-user-name": "Alice",
            "job-name": "Doc1"
        }, b"%PDF-1.7\n%%EOF\n")
        self.HandlerClass._handle_ipp(self.handler_instance, req1, {"client-address": "127.0.0.1"})

        # Create Job 2: Alice, pending (via create job)
        req2 = IppRequest(2, 0, Operation.CREATE_JOB, 2, {
            "requesting-user-name": "Alice",
            "job-name": "Doc2"
        }, b"")
        self.HandlerClass._handle_ipp(self.handler_instance, req2, {"client-address": "127.0.0.1"})

        # Create Job 3: Bob, pending (via create job)
        req3 = IppRequest(2, 0, Operation.CREATE_JOB, 3, {
            "requesting-user-name": "Bob",
            "job-name": "Doc3"
        }, b"")
        self.HandlerClass._handle_ipp(self.handler_instance, req3, {"client-address": "127.0.0.1"})

        # 1. completed jobs only
        req_completed = IppRequest(2, 0, Operation.GET_JOBS, 4, {"which-jobs": "completed"}, b"")
        res_comp = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_completed, {"client-address": "127.0.0.1"}))
        # res_comp.document will contain the job groups. Wait, since parse_request puts attributes in attributes dict, 
        # let's note that parse_request merges list attributes or we can inspect the raw/attributes.
        # Actually, let's verify if parse_request handles multiple groups:
        # In parse_request, if name in attributes: it makes it a list.
        # Let's see: Job 1 should be the only completed job.
        jobs_in_comp = res_comp.attributes.get("job-name")
        if isinstance(jobs_in_comp, list):
            self.assertEqual(len(jobs_in_comp), 1)
            self.assertEqual(jobs_in_comp[0], "Doc1")
        else:
            self.assertEqual(jobs_in_comp, "Doc1")

        # 2. active (not-completed) jobs only
        req_active = IppRequest(2, 0, Operation.GET_JOBS, 5, {"which-jobs": "not-completed"}, b"")
        res_act = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_active, {"client-address": "127.0.0.1"}))
        jobs_in_act = res_act.attributes.get("job-name")
        self.assertTrue(isinstance(jobs_in_act, list))
        self.assertEqual(len(jobs_in_act), 2)
        self.assertIn("Doc2", jobs_in_act)
        self.assertIn("Doc3", jobs_in_act)

        # 3. my-jobs filtering
        req_my = IppRequest(2, 0, Operation.GET_JOBS, 6, {
            "which-jobs": "not-completed",
            "my-jobs": True,
            "requesting-user-name": "Alice"
        }, b"")
        res_my = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_my, {"client-address": "127.0.0.1"}))
        jobs_in_my = res_my.attributes.get("job-name")
        # Since only Alice's pending job is Job 2, it should be single value or list of 1 element
        if isinstance(jobs_in_my, list):
            self.assertEqual(len(jobs_in_my), 1)
            self.assertEqual(jobs_in_my[0], "Doc2")
        else:
            self.assertEqual(jobs_in_my, "Doc2")

        # 4. limit
        req_limit = IppRequest(2, 0, Operation.GET_JOBS, 7, {
            "which-jobs": "not-completed",
            "limit": 1
        }, b"")
        res_lim = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_limit, {"client-address": "127.0.0.1"}))
        jobs_in_lim = res_lim.attributes.get("job-name")
        # Should be a single string doc name (since limit is 1)
        self.assertIsInstance(jobs_in_lim, str)

        # 5. requested-attributes
        req_attrs = IppRequest(2, 0, Operation.GET_JOBS, 8, {
            "which-jobs": "not-completed",
            "requested-attributes": ["job-id"]
        }, b"")
        res_att = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_attrs, {"client-address": "127.0.0.1"}))
        self.assertIn("job-id", res_att.attributes)
        self.assertNotIn("job-name", res_att.attributes)

    def test_get_printer_attributes_filtering(self):
        # 1. Request single attribute
        req = IppRequest(2, 0, Operation.GET_PRINTER_ATTRIBUTES, 1, {"requested-attributes": "printer-name"}, b"")
        res = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req, {"client-address": "127.0.0.1"}))
        self.assertIn("printer-name", res.attributes)
        self.assertNotIn("printer-info", res.attributes)

        # 2. Request group attribute "printer-description"
        req_group = IppRequest(2, 0, Operation.GET_PRINTER_ATTRIBUTES, 2, {"requested-attributes": "printer-description"}, b"")
        res_group = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_group, {"client-address": "127.0.0.1"}))
        self.assertIn("printer-name", res_group.attributes)
        self.assertIn("printer-info", res_group.attributes)
        self.assertNotIn("media-supported", res_group.attributes)

    def test_validate_job_attributes(self):
        # 1. Unsupported document-format with fidelity=True -> status 0x040B
        req_format_fail = IppRequest(2, 0, Operation.VALIDATE_JOB, 1, {
            "document-format": "text/plain",
            "ipp-attribute-fidelity": True
        }, b"")
        res = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_format_fail, {"client-address": "127.0.0.1"}))
        self.assertEqual(res.operation_id, Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED)

        # 2. Unsupported document-format with fidelity=False -> status 0x0002
        req_format_warn = IppRequest(2, 0, Operation.VALIDATE_JOB, 2, {
            "document-format": "text/plain",
            "ipp-attribute-fidelity": False
        }, b"")
        res2 = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_format_warn, {"client-address": "127.0.0.1"}))
        self.assertEqual(res2.operation_id, Status.SUCCESSFUL_OK_CONFLICTING_ATTRIBUTES)

        # 3. Unsupported media with fidelity=True -> status 0x040B
        req_media_fail = IppRequest(2, 0, Operation.VALIDATE_JOB, 3, {
            "media": "unknown-size",
            "ipp-attribute-fidelity": True
        }, b"")
        res3 = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_media_fail, {"client-address": "127.0.0.1"}))
        self.assertEqual(res3.operation_id, Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED)

        # 4. Invalid copies with fidelity=True -> status 0x040B
        req_copies_fail = IppRequest(2, 0, Operation.VALIDATE_JOB, 4, {
            "copies": 0,
            "ipp-attribute-fidelity": True
        }, b"")
        res4 = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_copies_fail, {"client-address": "127.0.0.1"}))
        self.assertEqual(res4.operation_id, Status.CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED)

    def test_create_send_workflow_job_states(self):
        # 1. Create-Job -> job state is pending (3)
        req_create = IppRequest(2, 0, Operation.CREATE_JOB, 1, {"requesting-user-name": "Test"}, b"")
        res_create = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_create, {"client-address": "127.0.0.1"}))
        self.assertEqual(res_create.operation_id, Status.SUCCESSFUL_OK)
        job_id = res_create.attributes["job-id"]
        self.assertEqual(res_create.attributes["job-state"], 3)  # pending

        # 2. Send-Document with last-document = False -> state transitions to processing (5)
        req_send1 = IppRequest(2, 0, Operation.SEND_DOCUMENT, 2, {
            "job-id": job_id,
            "last-document": False
        }, b"%PDF-1.7\n%%EOF\n")
        res_send1 = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_send1, {"client-address": "127.0.0.1"}))
        self.assertEqual(res_send1.attributes["job-state"], 5)  # processing

        # 3. Send-Document with last-document = True -> state transitions to completed (9)
        req_send2 = IppRequest(2, 0, Operation.SEND_DOCUMENT, 3, {
            "job-id": job_id,
            "last-document": True
        }, b"%PDF-1.7\n%%EOF\n")
        res_send2 = parse_request(self.HandlerClass._handle_ipp(self.handler_instance, req_send2, {"client-address": "127.0.0.1"}))
        self.assertEqual(res_send2.attributes["job-state"], 9)  # completed


if __name__ == "__main__":
    unittest.main()
