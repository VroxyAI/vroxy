import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
DOCTOR_SH = Path(__file__).resolve().parent / "docker_install_doctor.sh"


def run_sourced(snippet, path_dirs=(), env_extra=None, timeout=85):
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*map(str, path_dirs), env.get("PATH", "")])
    env["VROXY_INSTALL_NO_APT"] = "1"
    if env_extra:
        env.update(env_extra)
    script = f'source "{INSTALL_SH}"\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=timeout)


def fake_python3_without_ensurepip(bindir):
    real = shutil.which("python3")
    path = Path(bindir) / "python3"
    path.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        if [[ "$1" == "-m" && "$2" == "venv" ]]; then
          mkdir -p "$3/bin"
          ln -sf "{real}" "$3/bin/python"
          echo "The virtual environment was not created successfully because ensurepip is not available." >&2
          exit 1
        fi
        if [[ "$1" == "-m" && "$2" == "pip" ]]; then
          echo "/usr/bin/python3: No module named pip" >&2
          exit 1
        fi
        exec "{real}" "$@"
        """))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sourcing_runs_nothing(self):
        result = run_sourced("echo sourced-ok")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("sourced-ok", result.stdout.strip())

    def test_missing_ensurepip_names_the_fix_and_leaves_no_half_venv(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake_python3_without_ensurepip(bindir)
        venv = self.tmp / "venv"

        result = run_sourced(f'make_venv "{venv}"', path_dirs=[bindir])

        self.assertNotEqual(0, result.returncode)
        self.assertIn("can't create a virtualenv with pip", result.stderr)
        self.assertIn("run ./install.sh again", result.stderr)
        self.assertFalse(venv.exists(), "a failed venv must not be left for the next run to trust")

    def test_a_venv_without_pip_is_rebuilt_not_reused(self):
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                       check=True, timeout=60)
        self.assertNotEqual(0, subprocess.run([str(venv / "bin/python"), "-m", "pip", "--version"],
                                              capture_output=True, timeout=30).returncode)

        result = run_sourced(f'make_venv "{venv}"')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Replacing an incomplete virtualenv", result.stdout)
        check = subprocess.run([str(venv / "bin/python"), "-m", "pip", "--version"],
                               capture_output=True, timeout=30)
        self.assertEqual(0, check.returncode)

    def test_a_healthy_venv_is_left_alone(self):
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=60)
        marker = venv / "keep-me"
        marker.write_text("x")

        result = run_sourced(f'make_venv "{venv}"')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(marker.exists())
        self.assertNotIn("Creating virtualenv", result.stdout)

    def test_the_hint_names_a_package_manager_command(self):
        result = run_sourced("python_setup_hint")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.strip())
        self.assertIn("python3", result.stdout)

    def test_ensure_python_toolchain_refuses_without_apt_when_venv_broken(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake_python3_without_ensurepip(bindir)

        result = run_sourced("ensure_python_toolchain", path_dirs=[bindir])

        self.assertNotEqual(0, result.returncode)
        self.assertIn("can't create a virtualenv with pip", result.stderr)
        self.assertIn("python3-venv", result.stderr)

    def test_ensure_python_toolchain_is_a_no_op_when_venv_works(self):
        result = run_sourced("ensure_python_toolchain; echo ok")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ok", result.stdout.strip())

    def test_doctor_script_is_executable(self):
        self.assertTrue(DOCTOR_SH.is_file())
        self.assertTrue(os.access(DOCTOR_SH, os.X_OK), f"{DOCTOR_SH} must be executable")

    def test_read_prompt_strips_bracketed_paste(self):
        result = run_sourced(
            "read_prompt h \"host: \" <<< $'\\033[200~https://vroxy.ai\\033[201~'\n"
            "printf 'GOT=[%s]\\n' \"$h\""
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[https://vroxy.ai]", result.stdout.strip())

    def test_read_prompt_strips_bracketed_paste_from_a_silent_token(self):
        result = run_sourced(
            "read_prompt t \"token: \" silent <<< $'\\033[200~abc123token\\033[201~'\n"
            "printf 'GOT=[%s]\\n' \"$t\""
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[abc123token]", result.stdout.strip())

    def test_read_prompt_leaves_plain_input_alone(self):
        result = run_sourced(
            "read_prompt h \"host: \" <<< 'plain-host'\n"
            "printf 'GOT=[%s]\\n' \"$h\""
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[plain-host]", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
