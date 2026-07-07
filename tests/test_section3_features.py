import struct
import unittest
from datetime import datetime, timezone, timedelta
from archive_printer.ipp import GroupTag, IppRequest, ValueTag, parse_request, attributes, decode_value, encode_value


class Section3FeaturesTests(unittest.TestCase):
    def test_date_time_parsing_and_encoding(self):
        # 11-byte value: 2026-07-07T09:10:30.0+01:00
        raw_dt = struct.pack(">HBBBBBBcBB", 2026, 7, 7, 9, 10, 30, 0, b"+", 1, 0)
        decoded = decode_value(ValueTag.DATE_TIME, raw_dt)
        self.assertEqual(decoded, "2026-07-07T09:10:30.0+01:00")

        # Encode string
        encoded_str = encode_value(ValueTag.DATE_TIME, "2026-07-07T09:10:30.0+01:00")
        self.assertEqual(encoded_str, raw_dt)

        # Encode python datetime
        dt = datetime(2026, 7, 7, 9, 10, 30, tzinfo=timezone(timedelta(hours=1)))
        encoded_dt = encode_value(ValueTag.DATE_TIME, dt)
        self.assertEqual(encoded_dt, raw_dt)

        # 8-byte value: 2026-07-07T09:10:30.0
        raw_dt_8 = struct.pack(">HBBBBBB", 2026, 7, 7, 9, 10, 30, 0)
        decoded_8 = decode_value(ValueTag.DATE_TIME, raw_dt_8)
        self.assertEqual(decoded_8, "2026-07-07T09:10:30.0")

        encoded_str_8 = encode_value(ValueTag.DATE_TIME, "2026-07-07T09:10:30.0")
        self.assertEqual(encoded_str_8, raw_dt_8)

    def test_resolution_parsing_and_encoding(self):
        raw_res = struct.pack(">iiB", 300, 600, 3)
        decoded = decode_value(ValueTag.RESOLUTION, raw_res)
        self.assertEqual(decoded, "300x600 dpi")

        encoded = encode_value(ValueTag.RESOLUTION, "300x600 dpi")
        self.assertEqual(encoded, raw_res)

        # dpcm unit
        raw_res_dpcm = struct.pack(">iiB", 120, 120, 4)
        decoded_dpcm = decode_value(ValueTag.RESOLUTION, raw_res_dpcm)
        self.assertEqual(decoded_dpcm, "120x120 dpcm")

        encoded_dpcm = encode_value(ValueTag.RESOLUTION, "120x120 dpcm")
        self.assertEqual(encoded_dpcm, raw_res_dpcm)

    def test_range_of_integer_parsing_and_encoding(self):
        raw_range = struct.pack(">ii", 1, 100)
        decoded = decode_value(ValueTag.RANGE_OF_INTEGER, raw_range)
        self.assertEqual(decoded, (1, 100))

        encoded = encode_value(ValueTag.RANGE_OF_INTEGER, (1, 100))
        self.assertEqual(encoded, raw_range)

    def test_text_with_language_parsing_and_encoding(self):
        raw_twl = struct.pack(">H", 2) + b"en" + struct.pack(">H", 5) + b"hello"
        decoded = decode_value(ValueTag.TEXT_WITH_LANGUAGE, raw_twl)
        self.assertEqual(decoded, ("en", "hello"))

        encoded = encode_value(ValueTag.TEXT_WITH_LANGUAGE, ("en", "hello"))
        self.assertEqual(encoded, raw_twl)

    def test_octet_string_parsing_and_encoding(self):
        raw_val = b"hello octet string"
        decoded = decode_value(ValueTag.OCTET_STRING, raw_val)
        self.assertEqual(decoded, raw_val)

        encoded = encode_value(ValueTag.OCTET_STRING, raw_val)
        self.assertEqual(encoded, raw_val)

    def test_out_of_band_parsing_and_encoding(self):
        decoded = decode_value(ValueTag.UNKNOWN, b"")
        self.assertEqual(decoded, "out-of-band:18")

        encoded = encode_value(ValueTag.UNKNOWN, None)
        self.assertEqual(encoded, b"")

    def test_collection_parsing_and_encoding(self):
        # Construct raw collection bytes representing:
        # {
        #     "media-col": {
        #         "media-size": {
        #             "x-dimension": 21000,
        #             "y-dimension": 29700
        #         }
        #     }
        # }
        
        def attr_bytes(tag, name, val):
            name_b = name.encode()
            return bytes([tag]) + struct.pack(">H", len(name_b)) + name_b + struct.pack(">H", len(val)) + val

        # media-col (begCollection)
        payload = (
            struct.pack(">BBHI", 2, 0, 2, 7)
            + bytes([GroupTag.JOB_ATTRIBUTES])
            # media-col (begCollection)
            + attr_bytes(ValueTag.BEG_COLLECTION, "media-col", b"")
            # member-name: "media-size"
            + attr_bytes(ValueTag.MEMBER_ATTR_NAME, "", b"media-size")
            # media-size value (begCollection)
            + attr_bytes(ValueTag.BEG_COLLECTION, "", b"")
            # member-name: "x-dimension"
            + attr_bytes(ValueTag.MEMBER_ATTR_NAME, "", b"x-dimension")
            # value: 21000
            + attr_bytes(ValueTag.INTEGER, "", struct.pack(">i", 21000))
            # member-name: "y-dimension"
            + attr_bytes(ValueTag.MEMBER_ATTR_NAME, "", b"y-dimension")
            # value: 29700
            + attr_bytes(ValueTag.INTEGER, "", struct.pack(">i", 29700))
            # end nested collection
            + attr_bytes(ValueTag.END_COLLECTION, "", b"")
            # end parent collection
            + attr_bytes(ValueTag.END_COLLECTION, "", b"")
            + bytes([GroupTag.END])
        )

        request = parse_request(payload)
        expected = {
            "media-col": {
                "media-size": {
                    "x-dimension": 21000,
                    "y-dimension": 29700
                }
            }
        }
        self.assertEqual(request.attributes, expected)

        # Now encode it back using attributes helper
        encoded_chunks = attributes(GroupTag.JOB_ATTRIBUTES, [
            (ValueTag.BEG_COLLECTION, "media-col", expected["media-col"])
        ])
        encoded_bytes = b"".join(encoded_chunks)

        # The encoded chunks should match the attributes payload portion exactly!
        # The attributes payload portion excludes version header and GroupTag.END.
        expected_payload_portion = payload[8:-1]
        self.assertEqual(encoded_bytes, expected_payload_portion)


    def test_invalid_types_encoding_dont_crash(self):
        # 1. Resolution invalid tuple elements (non-integer)
        encoded_res = encode_value(ValueTag.RESOLUTION, ("abc", "def", "ghi"))
        # Should fall back to string encoding
        self.assertEqual(encoded_res, b"('abc', 'def', 'ghi')")

        # 2. RangeOfInteger invalid tuple elements (non-integer)
        encoded_range = encode_value(ValueTag.RANGE_OF_INTEGER, ("abc", "def"))
        self.assertEqual(encoded_range, b"('abc', 'def')")


if __name__ == "__main__":
    unittest.main()
