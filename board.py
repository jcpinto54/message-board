#!/usr/bin/env python3
"""Run the message board without installing it: python board.py <command> ..."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    if sys.argv[1:2] == ["hook"]:
        # Hooks run around every tool call, so they skip the CLI machinery.
        from msgboard.hooks import run

        sys.exit(run(sys.argv[2:]))

    from msgboard.cli import main

    sys.exit(main())
