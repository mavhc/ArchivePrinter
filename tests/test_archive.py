import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from archive_printer.archive import ArchiveStore, sanitize
from archive_printer.config import AppConfig, TimetableRule


class ArchiveStoreTests(unittest.TestCase):
    def test_stores_pdf_in_user_date_and_timetable_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            timezone = ZoneInfo("Europe/London")
            config = AppConfig(
                archive_root=Path(tmp),
                timezone=timezone,
                timetable=[
                    TimetableRule.from_mapping(
                        {
                            "users": ["John Smith"],
                            "days": ["monday"],
                            "start": "10:00",
                            "end": "11:00",
                            "folder": "Maths",
                        }
                    )
                ],
            )
            store = ArchiveStore(config)
            archived = store.store(
                b"%PDF-1.7\n%%EOF\n",
                {
                    "requesting-user-name": "John Smith",
                    "document-name": "Algebra worksheet",
                    "document-format": "application/pdf",
                },
                datetime(2026, 7, 6, 10, 30, tzinfo=timezone),
            )

            self.assertTrue(archived.pdf_path.exists())
            self.assertEqual(archived.pdf_path.parent, Path(tmp) / "John Smith" / "2026-07-06" / "Maths")
            self.assertIn("Algebra worksheet-20260706T103000000000.pdf", archived.pdf_path.name)
            self.assertTrue(archived.metadata_path.exists())

    def test_sanitize_removes_path_separators(self):
        self.assertEqual(sanitize("../Jane/Smith:Q1"), "Jane_Smith_Q1")


if __name__ == "__main__":
    unittest.main()
