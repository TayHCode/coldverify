import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import synthetic_policy
import verify_context

spec = importlib.util.spec_from_file_location(
    "linux_defaults", ROOT / "ci/linux-verify.py"
)
assert spec is not None and spec.loader is not None
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
REPO = "example-org/unlisted-default-routing"


class LinuxDefaults(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)
        for isolation in (
            patch.object(verify_context, "COLDBOX_HOST_FILE", Path("/nonexistent/coldbox/host")),
            patch.dict(os.environ, COLDBOX_GUEST="0"),
        ):
            isolation.start()
            self.addCleanup(isolation.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GIT_") and k != "GITHUB_REPOSITORY"
        }
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, env=self.env)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "remote",
                "add",
                "origin",
                f"https://github.com/{REPO}.git",
            ],
            check=True,
            env=self.env,
        )
        (self.root / ".fleet").mkdir()

    def invoke(self, *flags, guest=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {k: v for k, v in self.env.items() if not (guest and k == "COLDBOX_GUEST")}
        with (
            tempfile.TemporaryDirectory() as marker_dir,
            patch.object(verify_context, "COLDBOX_HOST_FILE", Path(marker_dir) / "host") as marker,
            patch.dict(os.environ, env, clear=True),
            patch.object(verify.platform, "node", return_value="runner-a"),
            patch.object(sys, "argv", ["linux-verify.py", str(self.root), *flags]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            if guest:
                marker.write_text(guest + "\n")
            code = verify.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_unlisted_owned_remote_forms_keep_their_repository_identity(self):
        for prefix in (
            "https://github.com/",
            "git@github.com:",
            "ssh://git@github.com/",
            "ssh://git@github.com:22/",
        ):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "remote",
                    "set-url",
                    "origin",
                    prefix + REPO + ".git",
                ],
                check=True,
                env=self.env,
            )
            with (
                self.subTest(prefix=prefix),
                patch.dict(os.environ, self.env, clear=True),
            ):
                self.assertEqual(verify.repository(self.root), REPO)

    def test_bash_recipe_runs_all_checks_and_propagates_failure_with_a_windows_wrapper(
        self,
    ):
        (self.root / ".fleet/verify.ps1").write_text('throw "wrong verifier"\n')
        for recipe_code, expected in ((0, 0), (7, 1)):
            (self.root / ".fleet/verify.sh").write_text(
                f'printf "full-recipe-ran\\n"\nexit {recipe_code}\n'
            )
            with self.subTest(recipe_code=recipe_code):
                code, stdout, stderr = self.invoke(guest="runner-a")
                self.assertEqual(code, expected, stdout + stderr)
                self.assertIn("full-recipe-ran", stdout)
        (self.root / ".gitignore").write_text(".fleet/verify.sh\n")
        code, stdout, stderr = self.invoke(guest="runner-a")
        self.assertEqual(code, 2, stdout + stderr)
        self.assertIn("complete .fleet/verify.sh or .fleet/verify.py", stderr)

    def test_unlisted_fork_falls_back_only_without_an_enrolled_native_remote(self):
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "remote",
                "add",
                "upstream",
                "https://github.com/example-org/mac-app.git",
            ],
            check=True,
            env=self.env,
        )
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(
                verify.repository(self.root), "example-org/mac-app"
            )
        code, stdout, stderr = self.invoke("--platform")
        self.assertEqual((code, stdout.strip(), stderr), (0, "macos", ""))
        subprocess.run(
            ["git", "-C", str(self.root), "remote", "remove", "upstream"],
            check=True,
            env=self.env,
        )
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(verify.repository(self.root), REPO)
        code, stdout, stderr = self.invoke("--platform")
        self.assertEqual((code, stdout.strip(), stderr), (0, "linux-generic", ""))

    def test_python_recipe_change_invalidates_a_cached_step(self):
        self.env.pop("COLD_VERIFY_NO_CACHE", None)
        self.env.pop("COLD_VERIFY_GATE", None)
        recipe = self.root / ".fleet/verify.py"
        (self.root / "input.txt").write_text("unchanged input\n")
        cache_command = [
            sys.executable,
            str(ROOT / "ci/verify_cache.py"),
            "step",
            "check",
            "--input",
            "input.txt",
            "--",
        ]
        self.env.update(
            COLD_VERIFY_CACHE_DIR=str(self.root / ".cold/cache"),
            COLD_VERIFY_HEAD="1" * 40,
        )
        for exit_code in (0, 7):
            recipe.write_text(
                "import subprocess, sys\n"
                f"raise SystemExit(subprocess.call({cache_command + [sys.executable, '-c', f'raise SystemExit({exit_code})']!r}))\n"
            )
            result = subprocess.run(
                [sys.executable, str(recipe)],
                cwd=self.root,
                env=self.env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode, exit_code, result.stdout + result.stderr
            )
            self.assertNotIn("::skip::", result.stdout)
            if exit_code == 0:
                cached = subprocess.run(
                    [sys.executable, str(recipe)],
                    cwd=self.root,
                    env=self.env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(cached.returncode, 0, cached.stdout + cached.stderr)
                self.assertIn("::skip::check", cached.stdout)

    def test_portable_python_recipe_runs_the_same_checks_as_its_windows_wrapper(self):
        (self.root / ".fleet/verify.ps1").write_text(
            '& python "$PSScriptRoot/verify.py"\nexit $LASTEXITCODE\n'
        )
        for recipe_code, expected in ((0, 0), (9, 1)):
            (self.root / ".fleet/verify.py").write_text(
                f'print("python-recipe-ran")\nraise SystemExit({recipe_code})\n'
            )
            with self.subTest(recipe_code=recipe_code):
                code, stdout, stderr = self.invoke(guest="runner-a")
                self.assertEqual(code, expected, stdout + stderr)
                self.assertIn("python-recipe-ran", stdout)
        (self.root / ".gitignore").write_text(".fleet/verify.py\n")
        code, stdout, stderr = self.invoke(guest="runner-a")
        self.assertEqual(code, 2, stdout + stderr)
        self.assertIn("complete .fleet/verify.sh or .fleet/verify.py", stderr)

    def test_generic_flag_cannot_drop_a_windows_only_recipe_or_dispatch_to_windows(
        self,
    ):
        (self.root / ".fleet/verify.ps1").write_text("exit 0\n")
        (self.root / "test_portable.py").write_text(
            "import unittest\nclass Portable(unittest.TestCase):\n    def test_pass(self):\n        self.assertEqual(2 + 2, 4)\n"
        )
        binary = self.root / "bin"
        binary.mkdir()
        marker = self.root / "ssh-called"
        (binary / "ssh").write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 91\n')
        (binary / "ssh").chmod(0o755)
        self.env.update(PATH=f"{binary}:{self.env['PATH']}", FLEET_CI_HOST="windows-a")
        code, stdout, stderr = self.invoke("--generic")
        self.assertEqual(code, 2, stdout + stderr)
        self.assertIn("complete .fleet/verify.sh or .fleet/verify.py", stderr)
        self.assertFalse(marker.exists())

    def remote_generic_after_documentation_change(self, assertion):
        (self.root / "legacy.ps1").write_text('throw "unchanged legacy file"\n')
        (self.root / "README.md").write_text("base\n")
        (self.root / "test_portable.py").write_text(
            "import unittest\nclass Portable(unittest.TestCase):\n"
            f"    def test_portable(self):\n        {assertion}\n"
        )
        for args in (
            ("add", "-A"),
            ("commit", "-qm", "base"),
            ("update-ref", "refs/remotes/origin/main", "HEAD"),
        ):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@localhost",
                    *args,
                ],
                check=True,
                capture_output=True,
                env=self.env,
            )
        (self.root / "README.md").write_text("changed documentation\n")
        with tempfile.TemporaryDirectory() as shim:
            shim = Path(shim)
            marker = shim / "ssh-called"
            (shim / "sitecustomize.py").write_text(
                synthetic_policy.SHIM + 'import platform\nplatform.node = lambda: "runner-a"\n'
            )
            ssh = shim / "ssh"
            ssh.write_text(
                f'#!/bin/sh\ntouch "{marker}"\nexport PYTHONPATH="{shim}"\n'
                'while [ "$1" = "-o" ]; do shift 2; done\nshift\nexec bash -c "$1"\n'
            )
            ssh.chmod(0o755)
            self.env.update(
                PATH=f"{shim}:{self.env['PATH']}",
                COLD_VERIFY_BACKEND="ssh:runner-a",
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="init.defaultBranch",
                GIT_CONFIG_VALUE_0="fixture-feature",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, self.env, clear=True),
                patch.object(
                    verify.platform, "node", return_value="fixture-controller"
                ),
                patch.object(sys, "argv", ["linux-verify.py", str(self.root)]),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertIsNone(verify.windows_lane.windows_reason(self.root))
                code = verify.main()
            self.assertTrue(marker.exists(), "verification did not dispatch over SSH")
        return code, stdout.getvalue() + stderr.getvalue()

    def test_remote_generic_verification_keeps_the_source_change_classification(self):
        code, output = self.remote_generic_after_documentation_change(
            "self.assertEqual(2 + 2, 4)"
        )
        self.assertEqual(code, 0, output)

    def test_remote_generic_verification_propagates_a_real_test_failure(self):
        code, output = self.remote_generic_after_documentation_change(
            "self.assertEqual(2 + 2, 5)"
        )
        self.assertEqual(code, 1, output)
