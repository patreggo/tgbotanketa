import tempfile
import unittest
from pathlib import Path

from app.database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "applicants.sqlite3")

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def test_tracks_application_lifecycle(self) -> None:
        self.database.begin_form(telegram_id=42, username="test", first_name="Telegram", last_name=None)
        self.database.save_first_name(42, "Анна")
        applicant = self.database.submit_form(42, "Иванова")
        self.assertEqual(applicant.full_name, "Иванова Анна")
        self.assertEqual(applicant.status, "registered")

        self.database.save_invite_link(42, "https://t.me/+example")
        applicant = self.database.mark_join_requested(42)
        self.assertEqual(applicant.status, "join_requested")

        applicant = self.database.mark_approved(42, approved_by=1)
        self.assertEqual(applicant.status, "approved")
        applicant = self.database.mark_member(42, is_member=True)
        self.assertEqual(applicant.status, "joined")
        applicant = self.database.mark_member(42, is_member=False)
        self.assertEqual(applicant.status, "left")


if __name__ == "__main__":
    unittest.main()
