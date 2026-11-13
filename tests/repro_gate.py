import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
spec = importlib.util.spec_from_file_location('verify_repro', ROOT / 'ci/verify_repro.py')
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)


class ReproGate:
    def gate(self, files):
        import subprocess
        import tempfile
        from unittest.mock import patch
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp.name, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                            *args], stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q', '-b', 'main')
        (root / '.gitignore').write_text('.cold/\n')
        (root / 'value.py').write_text('VALUE = 0\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('switch', '-qc', 'fix/value')
        (root / 'value.py').write_text('VALUE = 1\n')
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git('add', '.')
        git('commit', '-qm', 'Fix value')
        head = git('rev-parse', 'HEAD')
        with patch.dict('os.environ', {'GIT_DIR': '/nonexistent/cold-repro-git'}):
            code = repro.gate(root, True, None, ['Fix value', '', base, head])
        return code, json.loads((root / '.cold/verify-repro.json').read_text())
