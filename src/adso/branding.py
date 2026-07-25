"""Branded help and first-run welcome screens for the Adso CLI.

Pure presentation: no I/O, no argparse. Everything here is data plus string
rendering so it is trivial to unit-test and to keep in sync with the parser.
"""

from __future__ import annotations

LOGO = r"""
       ___      _
      / _ \    | |
     / /_\ \ __| |___  ___
     |  _  |/ _` / __|/ _ \
     | | | | (_| \__ \ (_) |
     \_| |_/\__,_|___/\___/
""".strip("\n")

TAGLINE = "Goodreads backup · physical shelf tracker · Notion export"

# Purpose-grouped command listing for the top-level help screen.
# Every subparser command MUST appear here exactly once; a test enforces it.
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Get started",
        [
            ("init", "Create your local catalogue"),
            ("doctor", "Check setup & show what to do next"),
            ("import", "Load a Goodreads CSV export"),
        ],
    ),
    (
        "Your library",
        [
            ("list", "Browse books in the catalogue"),
            ("search", "Find a book"),
            ("show", "Show details for one book"),
            ("edit", "Update format / tags / loaned-to / notes"),
        ],
    ),
    (
        "Keep it fresh",
        [
            ("sync", "Re-import & reconcile from source"),
            ("conflicts", "Review open sync conflicts"),
            ("resolve", "Decide a sync conflict"),
            ("fetch-covers", "Download missing cover art"),
            ("fetch-metadata", "Download descriptions & subjects"),
            ("set-cover", "Set a cover from URL or file"),
            ("dedupe", "Find duplicate books"),
        ],
    ),
    (
        "Share & config",
        [
            ("export", "Export to CSV, JSON, or Notion"),
            ("report", "Conflict / sync reports"),
            ("config", "Profiles & settings"),
            ("serve", "Open the web UI"),
            ("mcp", "Serve the catalogue to LLM agents (MCP)"),
        ],
    ),
]

GLOBAL_OPTIONS: list[tuple[str, str]] = [
    ("--db PATH", "SQLite database path (overrides the active profile)"),
    ("--profile NAME", "Configuration profile to use"),
    ("--version", "Show the Adso version and exit"),
    ("-h, --help", "Show this help and exit"),
]

EXAMPLES: list[str] = [
    "adso init",
    "adso import goodreads ~/Downloads/goodreads_library_export.csv",
    "adso list --status 'Read' --format physical",
    "adso serve",
]

FOOTER = "Run 'adso <command> --help' for details · 'adso doctor' if stuck."


def all_grouped_commands() -> set[str]:
    """Every command named in COMMAND_GROUPS (used by the drift-guard test)."""
    return {command for _heading, rows in COMMAND_GROUPS for command, _desc in rows}


def _format_two_columns(rows: list[tuple[str, str]], *, indent: str = "  ") -> list[str]:
    if not rows:
        return []
    width = max(len(left) for left, _ in rows)
    return [f"{indent}{left.ljust(width)}  {right}" for left, right in rows]


def render_help(prog: str = "adso") -> str:
    """The branded top-level help screen."""
    lines: list[str] = [LOGO, "", TAGLINE, "", "USAGE", f"  {prog} <command> [options]", ""]

    for heading, rows in COMMAND_GROUPS:
        lines.append(heading.upper())
        lines.extend(_format_two_columns(rows))
        lines.append("")

    lines.append("OPTIONS")
    lines.extend(_format_two_columns(GLOBAL_OPTIONS))
    lines.append("")

    lines.append("EXAMPLES")
    lines.extend(f"  {example}" for example in EXAMPLES)
    lines.append("")

    lines.append(FOOTER)
    return "\n".join(lines)


def render_welcome(*, csv_hint: str | None = None) -> str:
    """First-run quickstart shown when there is no catalogue and no config yet."""
    steps = [
        ("adso init", "Create your catalogue"),
        ("adso import goodreads <csv>", "Load your Goodreads export"),
        ("adso list", "Browse your books"),
    ]
    width = max(len(cmd) for cmd, _ in steps)
    step_lines = [
        f"  {index}. {cmd.ljust(width)}   {desc}"
        for index, (cmd, desc) in enumerate(steps, start=1)
    ]

    lines: list[str] = [
        LOGO,
        "",
        "Welcome to Adso — your local-first book catalogue.",
        "",
        "Let's get your library set up. Three steps:",
        "",
        *step_lines,
        "",
    ]

    if csv_hint:
        lines.extend(
            [
                "Found a Goodreads export nearby:",
                f"  adso import goodreads {csv_hint}",
                "",
            ]
        )

    lines.extend(
        [
            "Run 'adso doctor' anytime to check your setup.",
            "Run 'adso --help' to see all commands.",
        ]
    )
    return "\n".join(lines)
