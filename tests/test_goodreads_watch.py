"""Tests for Goodreads auto-sync: the Downloads watcher, ingest, and its LaunchAgents."""

from __future__ import annotations

import io
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from adso import cli, goodreads_watch, service
from adso.errors import AdsoError

FIXTURE = Path(__file__).resolve().parent.parent / "examples" / "goodreads_sample.csv"
CSV = b"Book Id,Title,Author\n1,Dune,Frank Herbert\n"


class HelperTests(unittest.TestCase):
    def test_find_exports_matches_browser_renamed_copies_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            first = d / "goodreads_library_export.csv"
            second = d / "goodreads_library_export (1).csv"
            for i, p in enumerate((first, second)):
                p.write_bytes(CSV)
                os.utime(p, (1000 + i, 1000 + i))
            (d / "goodreads_library_export.csv.crdownload").write_bytes(CSV)
            (d / "other.csv").write_bytes(CSV)
            self.assertEqual(goodreads_watch.find_exports(d), [first, second])

    def test_is_goodreads_export_checks_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "a.csv"
            good.write_bytes(b"\xef\xbb\xbf" + CSV)
            bad = Path(tmp) / "b.csv"
            bad.write_bytes(b"<html>nope</html>")
            self.assertTrue(goodreads_watch.is_goodreads_export(good))
            self.assertFalse(goodreads_watch.is_goodreads_export(bad))

    def test_archive_path_is_dated_and_never_clobbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = goodreads_watch.archive_path(tmp, date(2026, 9, 24))
            self.assertEqual(first.name, "goodreads-2026-09-24.csv")
            first.write_bytes(CSV)
            second = goodreads_watch.archive_path(tmp, date(2026, 9, 24))
            self.assertEqual(second.name, "goodreads-2026-09-24-2.csv")

    def test_archive_names_file_for_its_download_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "goodreads_library_export.csv"
            src.write_bytes(CSV)
            stamp = datetime(2026, 6, 4, 18, 19).timestamp()
            os.utime(src, (stamp, stamp))
            target = goodreads_watch.archive(src, Path(tmp) / "archive")
            self.assertEqual(target.name, "goodreads-2026-06-04.csv")
            self.assertFalse(src.exists())

    def test_backup_db_copies_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "adso.sqlite"
            conn = sqlite3.connect(src)
            conn.execute("create table t (x)")
            conn.execute("insert into t values (42)")
            conn.commit()
            conn.close()
            backup = goodreads_watch.backup_db(src, now=datetime(2026, 9, 24, 9, 0, 0))
            self.assertEqual(backup.name, "adso.sqlite.bak-20260924-090000")
            copy = sqlite3.connect(backup)
            self.assertEqual(copy.execute("select x from t").fetchone(), (42,))
            copy.close()

    def test_backup_db_skips_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(goodreads_watch.backup_db(Path(tmp) / "nope.sqlite"))

    def test_unreadable_folder_explains_privacy_settings(self):
        with mock.patch.object(goodreads_watch.Path, "iterdir", side_effect=PermissionError):
            with self.assertRaises(AdsoError) as ctx:
                goodreads_watch.check_readable("/Users/x/Downloads")
        self.assertIn("Privacy & Security", ctx.exception.hint)


class IngestTests(unittest.TestCase):
    """End-to-end `adso goodreads ingest` against a real temp catalogue."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.downloads = root / "Downloads"
        self.downloads.mkdir()
        self.db_path = root / "lib" / "cat.sqlite"
        self.db_path.parent.mkdir()
        self.archive_dir = self.db_path.parent / "exports" / "goodreads"
        self.home = root / "home"
        (self.home / ".Trash").mkdir(parents=True)
        for patch in (
            mock.patch.object(goodreads_watch, "notify"),
            mock.patch.object(goodreads_watch.Path, "home", return_value=self.home),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.notify = goodreads_watch.notify

    def _ingest(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr"):
            code = cli.main(
                [
                    "--db",
                    str(self.db_path),
                    "goodreads",
                    "ingest",
                    "--watch-dir",
                    str(self.downloads),
                    "--notify",
                    "--no-covers",
                    "--no-metadata",
                ]
            )
        return code, out.getvalue()

    def test_nothing_waiting_is_a_quiet_no_op(self):
        code, out = self._ingest()
        self.assertEqual(code, 0)
        self.assertIn("No Goodreads export waiting", out)
        self.notify.assert_not_called()
        self.assertFalse(self.db_path.exists())

    def test_export_is_synced_filed_away_and_notified(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertEqual(list(self.downloads.iterdir()), [])
        archived = list((self.db_path.parent / "exports" / "goodreads").glob("goodreads-*.csv"))
        self.assertEqual(len(archived), 1)
        conn = sqlite3.connect(self.db_path)
        self.assertGreater(conn.execute("select count(*) from books").fetchone()[0], 0)
        conn.close()
        self.assertIn("new", self.notify.call_args.args[0])

    def test_resync_backs_up_existing_catalogue_first(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        self._ingest()
        # A changed export (an identical one would be skipped as a duplicate).
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes() + b"\n")
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(list(self.db_path.parent.glob("cat.sqlite.bak-*"))), 1)

    def test_partial_and_foreign_files_are_left_alone(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(b"")
        (self.downloads / "goodreads_library_export (1).csv").write_bytes(b"<html/>")
        code, out = self._ingest()
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.downloads.iterdir())), 2)
        self.notify.assert_not_called()

    def _age(self, path, *, seconds_ago):
        stamp = datetime.now().timestamp() - seconds_ago
        os.utime(path, (stamp, stamp))

    def test_export_older_than_last_sync_is_skipped_and_filed(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        self._ingest()  # first sync: nothing to compare against, so it syncs
        old = self.downloads / "goodreads_library_export.csv"
        old.write_bytes(FIXTURE.read_bytes())
        self._age(old, seconds_ago=90 * 86400)
        self.notify.reset_mock()
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertIn("Skipped", out)
        self.assertEqual(list(self.downloads.iterdir()), [])
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("select count(*) from import_runs").fetchone()[0], 1)
        conn.close()
        self.assertIn("old Goodreads export", self.notify.call_args.args[0])
        self.assertEqual(len(list(self.db_path.parent.glob("cat.sqlite.bak-*"))), 0)

    def test_fresh_export_syncs_even_with_stale_ones_alongside(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        self._ingest()
        old = self.downloads / "goodreads_library_export.csv"
        old.write_bytes(FIXTURE.read_bytes())
        self._age(old, seconds_ago=90 * 86400)
        new = self.downloads / "goodreads_library_export (1).csv"
        new.write_bytes(FIXTURE.read_bytes() + b"\n")
        self._age(new, seconds_ago=-5)  # just downloaded
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertIn("Skipped goodreads_library_export.csv", out)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("select count(*) from import_runs").fetchone()[0], 2)
        conn.close()

    def test_first_ever_sync_never_counts_as_stale(self):
        old = self.downloads / "goodreads_library_export.csv"
        old.write_bytes(FIXTURE.read_bytes())
        self._age(old, seconds_ago=365 * 86400)
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Skipped", out)

    def test_redownload_of_last_synced_export_goes_to_trash(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        self._ingest()
        again = self.downloads / "goodreads_library_export.csv"
        again.write_bytes(FIXTURE.read_bytes())
        self._age(again, seconds_ago=-5)  # downloaded after the last sync
        self.notify.reset_mock()
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertIn("identical", out)
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.assertEqual(len(list((self.home / ".Trash").iterdir())), 1)
        self.assertEqual(len(list(self.archive_dir.glob("*.csv"))), 1)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("select count(*) from import_runs").fetchone()[0], 1)
        conn.close()
        self.assertIn("unchanged", self.notify.call_args.args[0])

    def test_changed_export_still_syncs(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        self._ingest()
        changed = self.downloads / "goodreads_library_export.csv"
        changed.write_bytes(FIXTURE.read_bytes() + b"\n")
        self._age(changed, seconds_ago=-5)
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertNotIn("identical", out)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("select count(*) from import_runs").fetchone()[0], 2)
        conn.close()

    def test_identical_to_a_failed_sync_is_not_skipped(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        with mock.patch.object(cli, "_sync_goodreads", side_effect=AdsoError("boom")):
            self._ingest()
        again = self.downloads / "goodreads_library_export.csv"
        again.write_bytes(FIXTURE.read_bytes())
        code, out = self._ingest()
        self.assertEqual(code, 0, out)
        self.assertNotIn("identical", out)

    def test_failed_sync_notifies_and_does_not_leave_file_to_retrigger(self):
        (self.downloads / "goodreads_library_export.csv").write_bytes(FIXTURE.read_bytes())
        with mock.patch.object(cli, "_sync_goodreads", side_effect=AdsoError("boom")):
            code, _ = self._ingest()
        self.assertEqual(code, 1)
        self.assertIn("boom", self.notify.call_args.args[0])
        self.assertEqual(list(self.downloads.iterdir()), [])


class SyncServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patches = [
            mock.patch.object(service.sys, "platform", "darwin"),
            mock.patch.object(service.Path, "home", return_value=self.home),
            mock.patch.object(service.subprocess, "run"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.run_mock = service.subprocess.run
        self.run_mock.side_effect = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1 if argv[1] == "print" else 0, "", ""
        )

    def test_watch_plist_argv_parses_as_ingest(self):
        spec = service.build_watch_plist(
            db_path="/tmp/x.sqlite", working_dir="/tmp", watch_dir="/tmp/dl"
        )
        args = cli._build_parser().parse_args(spec["ProgramArguments"][3:])
        self.assertEqual((args.command, args.action), ("goodreads", "ingest"))
        self.assertTrue(args.notify)
        self.assertEqual(spec["WatchPaths"], [args.watch_dir])
        self.assertNotIn("KeepAlive", spec)
        self.assertEqual(plistlib.loads(plistlib.dumps(spec)), spec)

    def test_remind_plist_is_weekly(self):
        spec = service.build_remind_plist(db_path="/tmp/x.sqlite", working_dir="/tmp", day="mon", hour=7)
        args = cli._build_parser().parse_args(spec["ProgramArguments"][3:])
        self.assertEqual((args.command, args.action), ("goodreads", "remind"))
        self.assertEqual(spec["StartCalendarInterval"], {"Weekday": 1, "Hour": 7, "Minute": 0})

    def test_install_status_uninstall_round_trip(self):
        service.install_sync(
            db_path=self.home / "c.sqlite", working_dir=self.home, watch_dir=self.home / "dl"
        )
        info = service.sync_status()
        self.assertTrue(info["installed"])
        self.assertEqual(info["watch_dir"], str((self.home / "dl").resolve()))
        self.assertEqual(info["reminder"], "weekly, Sun 09:00")
        self.assertTrue(service.uninstall_sync())
        self.assertFalse(service.agent_plist_path(service.WATCH_LABEL).exists())
        self.assertFalse(service.agent_plist_path(service.REMIND_LABEL).exists())

    def test_no_reminder_skips_reminder_agent(self):
        service.install_sync(
            db_path=self.home / "c.sqlite",
            working_dir=self.home,
            watch_dir=self.home / "dl",
            reminder=False,
        )
        self.assertIsNone(service.sync_status()["reminder"])


if __name__ == "__main__":
    unittest.main()
