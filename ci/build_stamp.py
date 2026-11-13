from pathlib import Path
import subprocess

from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def stamp(folder):
    found = subprocess.run(['git', 'describe', '--always', '--dirty', '--abbrev=12'], capture_output=True, text=True)
    if found.returncode == 0:
        (Path(folder) / 'COMMIT').write_text(found.stdout.strip() + '\n')


class StampBuild(build_py):
    def run(self):
        super().run()
        stamp(Path(self.build_lib) / 'coldverify')


class StampSdist(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        stamp(base_dir)

