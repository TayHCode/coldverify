import os
from pathlib import Path
import sys


def main():
    script = Path(__file__).resolve().parent.parent / 'coldverify'
    os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])
