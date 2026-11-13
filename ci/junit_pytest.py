#!/usr/bin/env python3
"""Run pytest and write a signed JUnit XML report into $COLD_VERIFY_JUNIT_DIR.

    "$python" junit_pytest.py [pytest arguments]

Run it with the Python that has pytest installed. A drop-in for `python -m pytest`; the exit code is pytest's.
Docs: docs/verify-trace.md
"""
import os
import sys
import uuid
from pathlib import Path

from junit_unittest import sign, take_junit_environment


def main(argv=None):
    junit, key = take_junit_environment()
    sys.path[0] = os.getcwd()
    import pytest
    report = Path(junit) / f'pytest-{uuid.uuid4().hex}.xml' if junit else None
    code = pytest.main([*(sys.argv[1:] if argv is None else argv), *([f'--junit-xml={report}'] if report else [])])
    if report:
        sign(report, key)
    return int(code)


if __name__ == '__main__':
    sys.exit(main())
