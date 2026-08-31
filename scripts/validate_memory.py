#!/usr/bin/env python3
"""Compatibility wrapper for ``memoryctl validate``."""

from __future__ import annotations

import sys

from memoryctl import main


if __name__ == "__main__":
    raise SystemExit(main(["validate", *sys.argv[1:]]))
