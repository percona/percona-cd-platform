"""Module entry point: `python3 -m cdpctl` (used by the CI stdlib path)."""

from cdpctl.cli import main

raise SystemExit(main())
