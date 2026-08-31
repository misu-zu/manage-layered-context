#!/usr/bin/env python3
"""Compatibility wrapper for ``memoryctl recall``."""

from __future__ import annotations

import sys

from memoryctl import main


if __name__ == "__main__":
    raise SystemExit(main(["recall", *sys.argv[1:]]))
