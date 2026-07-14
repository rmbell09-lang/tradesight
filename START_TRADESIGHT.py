#!/usr/bin/env python3
"""Compatibility launcher for the supported TradeSight CLI."""

from tradesight.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
