"""Test runner: python3 run_tests.py (equivalent to pytest -q)."""
import sys

import pytest

if __name__ == "__main__":
    sys.exit(pytest.main(["-q", "--rootdir=."] + sys.argv[1:]))
