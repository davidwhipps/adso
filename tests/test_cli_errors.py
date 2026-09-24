"""Tests that the CLI fails humanely — clean messages, no tracebacks, useful exit codes."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from adso import cli


class CliErrorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = str(self.root / "cat.sqlite")
        # Isolate config: empty environment with a throwaway XDG home so no real
        # user profile or Notion credentials leak in.
        self._env = mock.patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": str(self.root / "xdg")}, clear=True
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_missing_csv_is_clean(self):
        code, _out, err = self._run(["import", "goodreads", str(self.root / "nope.csv")])
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        self.assertIn("Could not find a Goodreads CSV", err)
        self.assertNotIn("Traceback", err)

    def test_notion_without_credentials_is_clean(self):
        self._run(["init"])
        code, _out, err = self._run(["export", "notion", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        self.assertIn("NOTION_API_KEY", err)
        self.assertNotIn("Traceback", err)

    def test_resolve_unknown_id_is_clean(self):
        self._run(["init"])
        code, _out, err = self._run(["resolve", "424242"])
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        self.assertIn("adso conflicts", err)
        self.assertNotIn("Traceback", err)

    def test_edit_without_fields_is_usage_error(self):
        self._run(["init"])
        # Usage-shaped problems stay with argparse (exit 2), not the error boundary.
        with self.assertRaises(SystemExit) as ctx:
            self._run(["edit", "123"])
        self.assertEqual(ctx.exception.code, 2)

    def test_edit_unknown_id_is_clean(self):
        self._run(["init"])
        code, _out, err = self._run(["edit", "424242", "--local-notes", "x"])
        self.assertEqual(code, 1)
        self.assertIn("Error: No book found for Goodreads ID 424242", err)
        self.assertIn("Next:", err)
        self.assertNotIn("Traceback", err)

    def test_successful_command_returns_zero(self):
        code, out, _err = self._run(["init"])
        self.assertEqual(code, 0)
        self.assertIn("Initialized", out)


if __name__ == "__main__":
    unittest.main()
