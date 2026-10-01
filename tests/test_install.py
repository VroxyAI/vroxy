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

    def test_read_prompt_masked_echoes_stars_and_keeps_the_value(self):
        result = run_sourced(
            "read_prompt t \"token: \" masked <<< 'abc123token'\n"
            "printf 'GOT=[%s]\\n' \"$t\""
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[abc123token]", result.stdout.strip())
        self.assertIn("***********", result.stderr)

    def test_read_prompt_masked_strips_bracketed_paste(self):
        result = run_sourced(
            "read_prompt t \"token: \" masked <<< $'\\033[200~abc123token\\033[201~'\n"
            "printf 'GOT=[%s]\\n' \"$t\""
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[abc123token]", result.stdout.strip())

    def test_choose_project_defaults_to_the_only_folder(self):
        (self.tmp / "repo-a").mkdir()
        result = run_sourced(
            f'choose_project p "{self.tmp}" <<< ""\n'
            'printf "GOT=[%s]\\n" "$p"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[repo-a]", result.stdout.strip())

    def test_choose_project_lists_a_monorepo_and_picks_by_number(self):
        (self.tmp / "aa").mkdir()
        (self.tmp / "bb").mkdir()
        result = run_sourced(
            f'choose_project p "{self.tmp}" <<< "2"\n'
            'printf "GOT=[%s]\\n" "$p"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Found 2 folders", result.stdout)
        self.assertTrue(result.stdout.strip().endswith("GOT=[bb]"), result.stdout)

    def test_choose_project_accepts_a_folder_name(self):
        (self.tmp / "aa").mkdir()
        (self.tmp / "bb").mkdir()
        result = run_sourced(
            f'choose_project p "{self.tmp}" <<< "aa"\n'
            'printf "GOT=[%s]\\n" "$p"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.strip().endswith("GOT=[aa]"), result.stdout)

    def test_choose_project_asks_when_no_folders_exist(self):
        result = run_sourced(
            f'choose_project p "{self.tmp}" <<< "myproj"\n'
            'printf "GOT=[%s]\\n" "$p"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("GOT=[myproj]", result.stdout.strip())

    def test_repo_resolves_accepts_parent_and_rejects_repo_as_root(self):
        parent = self.tmp / "brevitas"
        (parent / "brev72true").mkdir(parents=True)

        ok = run_sourced(f'repo_resolves "{parent}" brev72true && echo yes || echo no')
        self.assertEqual(0, ok.returncode, ok.stderr)
        self.assertEqual("yes", ok.stdout.strip())

        bad = run_sourced(f'repo_resolves "{parent / "brev72true"}" brev72true && echo yes || echo no')
        self.assertEqual(0, bad.returncode, bad.stderr)
        self.assertEqual("no", bad.stdout.strip())

    def test_is_git_repo_true_for_a_checkout_and_false_for_a_plain_dir(self):
        parent = self.tmp / "brevitas"
        (parent / "brev72true" / ".git").mkdir(parents=True)

        yes = run_sourced(f'is_git_repo "{parent / "brev72true"}" && echo yes || echo no')
        self.assertEqual(0, yes.returncode, yes.stderr)
        self.assertEqual("yes", yes.stdout.strip())

        no = run_sourced(f'is_git_repo "{parent}" && echo yes || echo no')
        self.assertEqual(0, no.returncode, no.stderr)
        self.assertEqual("no", no.stdout.strip())

    def test_sanitize_instance_id_maps_unsafe_chars_to_dash(self):
        result = run_sourced('sanitize_instance_id "My Agent 2.0"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("My-Agent-2.0", result.stdout.strip())

    def test_env_field_reads_a_key_from_env_contents(self):
        result = run_sourced(
            "v=$(printf 'CODE_ROOT=/a/b\\nPROJECT=web\\n')\n"
            "env_field \"$v\" CODE_ROOT"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("/a/b", result.stdout.strip())

    def test_env_field_returns_empty_for_a_missing_key(self):
        result = run_sourced(
            "v=$(printf 'CODE_ROOT=/a/b\\n')\n"
            "env_field \"$v\" PROJECT"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_whoami_agents_lists_names_and_online_status(self):
        body = ('{"agents": ['
                '{"name": "Claude Code", "kind": "claude_code", "online": true},'
                '{"name": "GitHub", "kind": "copilot", "online": false}]}')
        result = run_sourced(f'v={body!r}\nwhoami_agents <<< "$v"')
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(["Claude Code\tready", "GitHub\toffline"], lines)

    def test_whoami_agents_is_empty_for_no_agents(self):
        result = run_sourced('whoami_agents <<< \'{"agents": []}\'')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_harness_bin_installed_finds_one_on_path(self):
        result = run_sourced("harness_bin_installed python3 && echo yes || echo no")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("yes", result.stdout.strip())

    def test_harness_bin_installed_finds_one_in_home_local_bin(self):
        bindir = self.tmp / ".local" / "bin"
        bindir.mkdir(parents=True)
        (bindir / "someharness").write_text("#!/bin/sh\n")
        (bindir / "someharness").chmod(0o755)
        result = run_sourced(
            "harness_bin_installed someharness && echo yes || echo no",
            env_extra={"HOME": str(self.tmp)})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("yes", result.stdout.strip())

    def test_harness_bin_installed_misses_an_absent_one(self):
        result = run_sourced(
            "harness_bin_installed definitely-not-a-real-harness-xyz && echo yes || echo no")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("no", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
