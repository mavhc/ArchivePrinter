import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from archive_printer.config import AppConfig
from archive_printer.ipp import GroupTag, IppRequest, Operation, parse_request
from archive_printer.server import ArchivePrinterApp, make_handler
from archive_printer.mdns import build_advertisement


class Section4FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dynamic_security_and_auth_attributes(self):
        # 1. Test with Basic Auth and TLS enabled
        config_secure = AppConfig(
            archive_root=Path(self.tmp_dir.name),
            timezone=ZoneInfo("UTC"),
            require_basic_auth=True,
            enable_tls=True
        )
        app_secure = ArchivePrinterApp(config_secure)
        HandlerClass = make_handler(app_secure)
        handler_instance = HandlerClass.__new__(HandlerClass)
        handler_instance.path = "/ipp/print"
        handler_instance.headers = {"Host": "localhost:8631"}

        req = IppRequest(2, 0, Operation.GET_PRINTER_ATTRIBUTES, 1, {}, b"")
        res_bytes = HandlerClass._handle_ipp(handler_instance, req, {"client-address": "127.0.0.1"})
        res = parse_request(res_bytes)

        self.assertEqual(res.attributes["uri-authentication-supported"], "basic")
        self.assertEqual(res.attributes["uri-security-supported"], "tls")
        self.assertTrue(res.attributes["printer-uri-supported"].startswith("ipps://"))
        self.assertTrue(res.attributes["printer-more-info"].startswith("https://"))

        # 2. Test with Basic Auth and TLS disabled (default)
        config_plain = AppConfig(
            archive_root=Path(self.tmp_dir.name),
            timezone=ZoneInfo("UTC"),
            require_basic_auth=False,
            enable_tls=False
        )
        app_plain = ArchivePrinterApp(config_plain)
        HandlerClassPlain = make_handler(app_plain)
        handler_instance_plain = HandlerClassPlain.__new__(HandlerClassPlain)
        handler_instance_plain.path = "/ipp/print"
        handler_instance_plain.headers = {"Host": "localhost:8631"}

        req2 = IppRequest(2, 0, Operation.GET_PRINTER_ATTRIBUTES, 2, {}, b"")
        res_bytes2 = HandlerClassPlain._handle_ipp(handler_instance_plain, req2, {"client-address": "127.0.0.1"})
        res2 = parse_request(res_bytes2)

        self.assertEqual(res2.attributes["uri-authentication-supported"], "none")
        self.assertEqual(res2.attributes["uri-security-supported"], "none")
        self.assertTrue(res2.attributes["printer-uri-supported"].startswith("ipp://"))
        self.assertTrue(res2.attributes["printer-more-info"].startswith("http://"))

    def test_mdns_advertisement_updates(self):
        # 1. TLS enabled
        config_tls = AppConfig(
            archive_root=Path(self.tmp_dir.name),
            timezone=ZoneInfo("UTC"),
            enable_tls=True
        )
        adv_tls = build_advertisement(config_tls)
        self.assertEqual(adv_tls.service_type, "_ipps._tcp.local.")
        self.assertTrue(adv_tls.service_name.endswith("._ipps._tcp.local."))
        self.assertTrue(adv_tls.properties["adminurl"].startswith("https://"))

        # 2. TLS disabled
        config_no_tls = AppConfig(
            archive_root=Path(self.tmp_dir.name),
            timezone=ZoneInfo("UTC"),
            enable_tls=False
        )
        adv_no_tls = build_advertisement(config_no_tls)
        self.assertEqual(adv_no_tls.service_type, "_ipp._tcp.local.")
        self.assertTrue(adv_no_tls.service_name.endswith("._ipp._tcp.local."))
        self.assertTrue(adv_no_tls.properties["adminurl"].startswith("http://"))


if __name__ == "__main__":
    unittest.main()
