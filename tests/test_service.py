"""Tests for the macOS always-on service (LaunchAgent) helpers."""

from __future__ import annotations

import io
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from adso import cli, service
from adso.errors import AdsoError


class BuildPlistTests(unittest.TestCase):
    def test_plist_runs_serve_headless_on_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = service.build_plist(
                db_path=Path(tmp) / "cat.sqlite",
                port=9123,
                working_dir=tmp,
                python="/opt/py/bin/python",
                logs=Path(tmp) / "logs",
            )
            argv = spec["ProgramArguments"]
            self.assertEqual(argv[:3], ["/opt/py/bin/python", "-m", "adso.cli"])
            db_arg = argv[argv.index("--db") + 1]
            self.assertTrue(Path(db_arg).is_absolute())
            self.assertEqual(argv[argv.index("--port") + 1], "9123")
            self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
            self.assertIn("--no-browser", argv)
            # --db is a global option and must precede the subcommand.
            self.assertLess(argv.index("--db"), argv.index("serve"))
            self.assertTrue(spec["RunAtLoad"])
            self.assertTrue(spec["KeepAlive"])
            self.assertEqual(spec["Label"], service.LABEL)
            # Round-trips as a valid plist.
            self.assertEqual(plistlib.loads(plistlib.dumps(spec)), spec)

    def test_plist_argv_parses_as_a_serve_command(self):
        spec = service.build_plist(db_path="/tmp/x.sqlite", port=8420, working_dir="/tmp")
        args = cli._build_parser().parse_args(spec["ProgramArguments"][3:])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.port, 8420)
        self.assertTrue(args.no_browser)


class PlatformGuardTests(unittest.TestCase):
    def test_non_macos_is_refused(self):
        with mock.patch.object(service.sys, "platform", "linux"):
            for call in (service.uninstall, service.restart, service.status):
                with self.assertRaises(AdsoError):
                    call()


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patches = [
            mock.patch.object(service.sys, "platform", "darwin"),
            mock.patch.object(service.Path, "home", return_value=self.home),
            mock.patch.object(service.subprocess, "run"),
            mock.patch.object(service, "_port_in_use", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.run_mock = service.subprocess.run

    def _launchctl_calls(self):
        return [c.args[0][1] for c in self.run_mock.call_args_list]

    def test_install_writes_plist_and_bootstraps(self):
        # "print" fails (not loaded yet); everything else succeeds.
        self.run_mock.side_effect = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1 if argv[1] == "print" else 0, "", ""
        )
        url = service.install(db_path=self.home / "cat.sqlite", port=8420, working_dir=self.home)
        self.assertEqual(url, "http://127.0.0.1:8420")
        self.assertTrue(service.plist_path().exists())
        self.assertIn("bootstrap", self._launchctl_calls())
        self.assertNotIn("bootout", self._launchctl_calls())

    def test_reinstall_boots_out_existing_first(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        service.install(db_path=self.home / "cat.sqlite", port=8420, working_dir=self.home)
        calls = self._launchctl_calls()
        self.assertLess(calls.index("bootout"), calls.index("bootstrap"))

    def test_busy_port_is_refused_before_writing_plist(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(service, "_port_in_use", return_value=True):
            with self.assertRaises(AdsoError) as ctx:
                service.install(db_path=self.home / "cat.sqlite", port=8420, working_dir=self.home)
        self.assertIn("8420", str(ctx.exception))
        self.assertFalse(service.plist_path().exists())

    def test_bootstrap_failure_surfaces_as_adso_error(self):
        self.run_mock.side_effect = lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1 if argv[1] in ("print", "bootstrap") else 0, "", "boom"
        )
        with self.assertRaises(AdsoError) as ctx:
            service.install(db_path=self.home / "cat.sqlite", port=8420, working_dir=self.home)
        self.assertIn("boom", str(ctx.exception))

    def test_status_reports_url_and_db_from_plist(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 1, "", "")
        path = service.plist_path()
        path.parent.mkdir(parents=True)
        with path.open("wb") as fh:
            plistlib.dump(
                service.build_plist(db_path="/data/cat.sqlite", port=8420, working_dir="/"), fh
            )
        info = service.status()
        self.assertTrue(info["installed"])
        self.assertFalse(info["loaded"])
        self.assertEqual(info["url"], "http://127.0.0.1:8420")
        self.assertEqual(info["db"], str(Path("/data/cat.sqlite").resolve()))

    def test_cli_status_when_not_installed(self):
        self.run_mock.return_value = subprocess.CompletedProcess([], 1, "", "")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--db", str(self.home / "cat.sqlite"), "service"])
        self.assertEqual(code, 0)
        self.assertIn("not installed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
