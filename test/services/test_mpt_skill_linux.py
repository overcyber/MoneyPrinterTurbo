import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_PATH = Path(__file__).resolve().parents[2] / "docs" / "skill" / "mpt_skill.py"
SPEC = importlib.util.spec_from_file_location("mpt_skill_linux_test_module", SKILL_PATH)
mpt_skill = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mpt_skill)


class MoneyPrinterTurboSkillLinuxTests(unittest.TestCase):
    def test_linux_uses_linux_compose_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "docker-compose.linux.yml").write_text("services: {}\n")
            (root / "docker-compose.providers.yml").write_text("services: {}\n")
            with mock.patch.object(mpt_skill.sys, "platform", "linux"):
                files = mpt_skill.docker_compose_files(root)
        self.assertEqual(
            files,
            ["docker-compose.linux.yml", "docker-compose.providers.yml"],
        )

    def test_non_linux_keeps_regular_compose_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "docker-compose.yml").write_text("services: {}\n")
            (root / "docker-compose.providers.yml").write_text("services: {}\n")
            with mock.patch.object(mpt_skill.sys, "platform", "darwin"):
                files = mpt_skill.docker_compose_files(root)
        self.assertEqual(
            files,
            ["docker-compose.yml", "docker-compose.providers.yml"],
        )

    def test_auto_transport_prefers_existing_api(self):
        args = mock.Mock(transport="auto", api_base_url="http://127.0.0.1:8080")
        with mock.patch.object(mpt_skill, "api_is_ready", return_value=True):
            with mock.patch.object(mpt_skill, "is_linux", return_value=True):
                self.assertEqual(mpt_skill.resolve_transport(args), "api")

    def test_auto_transport_uses_docker_on_linux_when_api_is_absent(self):
        args = mock.Mock(transport="auto", api_base_url="http://127.0.0.1:8080")
        with mock.patch.object(mpt_skill, "api_is_ready", return_value=False):
            with mock.patch.object(mpt_skill, "is_linux", return_value=True):
                self.assertEqual(mpt_skill.resolve_transport(args), "docker")

    def test_start_docker_api_uses_linux_host_network_compose(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "docker-compose.linux.yml").write_text("services: {}\n")
            (root / "docker-compose.providers.yml").write_text("services: {}\n")
            with mock.patch.object(mpt_skill.sys, "platform", "linux"):
                with mock.patch.object(
                    mpt_skill.shutil, "which", return_value="/usr/bin/docker"
                ):
                    with mock.patch.object(mpt_skill.subprocess, "run") as run:
                        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                        mpt_skill.start_docker_api(root)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["docker", "compose", "version"], commands)
        self.assertIn(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.linux.yml",
                "-f",
                "docker-compose.providers.yml",
                "up",
                "-d",
                "--build",
                "api",
            ],
            commands,
        )


if __name__ == "__main__":
    unittest.main()
