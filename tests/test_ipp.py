import struct
import unittest

from archive_printer.ipp import GroupTag, Operation, ValueTag, parse_request, response, Status
from archive_printer.config import AppConfig
from archive_printer.mdns import build_advertisement
from archive_printer.server import DEFAULT_MEDIA, ISO_A_MEDIA, ISO_B_MEDIA, printer_attribute_groups


def attr(tag, name, value):
    name_bytes = name.encode()
    value_bytes = value.encode()
    return bytes([tag]) + struct.pack(">H", len(name_bytes)) + name_bytes + struct.pack(">H", len(value_bytes)) + value_bytes


class IppTests(unittest.TestCase):
    def test_parse_print_job_with_pdf_payload(self):
        payload = (
            struct.pack(">BBHI", 2, 0, Operation.PRINT_JOB, 7)
            + bytes([GroupTag.OPERATION_ATTRIBUTES])
            + attr(ValueTag.CHARSET, "attributes-charset", "utf-8")
            + attr(ValueTag.NATURAL_LANGUAGE, "attributes-natural-language", "en")
            + bytes([GroupTag.JOB_ATTRIBUTES])
            + attr(ValueTag.NAME_WITHOUT_LANGUAGE, "requesting-user-name", "Jane Smith")
            + attr(ValueTag.NAME_WITHOUT_LANGUAGE, "job-name", "Homework")
            + bytes([GroupTag.END])
            + b"%PDF-1.7\n%%EOF\n"
        )

        request = parse_request(payload)

        self.assertEqual(request.operation, Operation.PRINT_JOB)
        self.assertEqual(request.request_id, 7)
        self.assertEqual(request.attributes["requesting-user-name"], "Jane Smith")
        self.assertEqual(request.document, b"%PDF-1.7\n%%EOF\n")

    def test_response_keeps_request_id(self):
        request = parse_request(
            struct.pack(">BBHI", 2, 0, Operation.GET_PRINTER_ATTRIBUTES, 42)
            + bytes([GroupTag.OPERATION_ATTRIBUTES])
            + bytes([GroupTag.END])
        )

        payload = response(request, Status.SUCCESSFUL_OK)

        self.assertEqual(payload[:8], struct.pack(">BBHI", 2, 0, Status.SUCCESSFUL_OK, 42))

    def test_printer_attributes_use_supplied_uri(self):
        config = AppConfig.load("missing-test-config.json")

        groups = printer_attribute_groups(config, "ipp://127.0.0.1:8631/ipp/print")

        printer_attrs = dict((name, value) for _tag, name, value in groups[0][1])
        self.assertEqual(printer_attrs["printer-uri-supported"], "ipp://127.0.0.1:8631/ipp/print")
        self.assertIn("application/pdf", printer_attrs["document-format-supported"])

    def test_printer_attributes_advertise_iso_a_and_b_media(self):
        config = AppConfig.load("missing-test-config.json")

        groups = printer_attribute_groups(config, "ipp://127.0.0.1:8631/ipp/print")

        printer_attrs = dict((name, value) for _tag, name, value in groups[0][1])
        self.assertEqual(printer_attrs["media-default"], DEFAULT_MEDIA)
        self.assertEqual(printer_attrs["media-supported"], ISO_A_MEDIA + ISO_B_MEDIA)
        self.assertIn("iso_a0_841x1189mm", printer_attrs["media-supported"])
        self.assertIn("iso_a10_26x37mm", printer_attrs["media-supported"])
        self.assertIn("iso_b0_1000x1414mm", printer_attrs["media-supported"])
        self.assertIn("iso_b10_31x44mm", printer_attrs["media-supported"])

    def test_mdns_advertisement_uses_ipp_service_and_resource_path(self):
        config = AppConfig.load("missing-test-config.json")

        advertisement = build_advertisement(config)

        self.assertEqual(advertisement.service_type, "_ipp._tcp.local.")
        self.assertEqual(advertisement.server_name, "archive-printer.local.")
        self.assertEqual(advertisement.port, 8631)
        self.assertEqual(advertisement.properties["rp"], "ipp/print")
        self.assertEqual(advertisement.properties["pdl"], "application/pdf")


if __name__ == "__main__":
    unittest.main()
