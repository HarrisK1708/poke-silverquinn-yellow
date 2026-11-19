#!/usr/bin/env python3
"""
Headless proof of the step-3 Mew static encounter. Superseded by
tools/verify_statics.py, which runs the same warp-and-place harness for every
static (SILVER, QUINN, RAYA, MEW) with stricter checks (no move may be used
before the ball: a Mew that Transforms is caught as a Ditto) plus a save round
trip. This wrapper keeps the old command working: it runs only the MEW case.

Usage: python3 tools/verify_mew.py [--rom pokeyellow.gbc] [--screenshot-dir DIR]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_statics  # noqa: E402

if __name__ == "__main__":
    sys.argv[1:1] = ["--only", "MEW"]
    sys.exit(verify_statics.main())
