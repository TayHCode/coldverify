import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1] / 'ci'
sys.path.insert(0, str(CI))
import verify_policy

POLICY = Path(__file__).resolve().parent / 'fixtures' / 'verification-platforms.json'
SHIM = ('import importlib.util, sys\nfrom pathlib import Path\n'
        f'_spec = importlib.util.spec_from_file_location("verify_policy", {str(CI / "verify_policy.py")!r})\n'
        '_policy = importlib.util.module_from_spec(_spec)\n_spec.loader.exec_module(_policy)\n'
        f'_policy.PLATFORMS = Path({str(POLICY)!r})\nsys.modules["verify_policy"] = _policy\n')


def use(add_cleanup):
    patcher = patch.object(verify_policy, 'PLATFORMS', POLICY)
    patcher.start()
    add_cleanup(patcher.stop)


def shim_folder(add_cleanup):
    folder = tempfile.TemporaryDirectory()
    add_cleanup(folder.cleanup)
    (Path(folder.name) / 'sitecustomize.py').write_text(SHIM)
    return folder.name
