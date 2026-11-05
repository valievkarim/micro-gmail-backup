"""A fresh copy of the project installs into a new venv with uv and passes baseline tests there."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

BASELINE = [
    "tests/test_eml_text.py",
    "tests/test_repeated_refresh.py",
    "tests/test_backup.py::test_functions_are_typed_and_beartype_rejects_wrong_argument_types",
    "tests/test_backup.py::test_lists_all_pages_with_spam_trash_and_saves_request_evidence",
    "tests/test_backup.py::test_rerun_verifies_and_skips_existing_bodies",
    "tests/test_backup.py::test_corrupt_truncated_or_trailing_data_is_preserved_and_fails",
    "tests/test_backup.py::test_recorded_quota_403_names_the_quota_and_where_to_raise_it",
]


@pytest.mark.parametrize(
    "sync_args",
    [
        pytest.param(["--locked"], id="locked"),
        # The lowest versions pyproject.toml allows, on the oldest Python it claims to support.
        pytest.param(["--resolution", "lowest-direct", "--python", "3.11"], id="lowest-py311"),
    ],
)
def test_fresh_install_passes_baseline_tests(tmp_path: Path, sync_args: list) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ["pyproject.toml", "uv.lock", "micro_gmail_backup.py", "eml_text.py"]:
        shutil.copy2(ROOT / name, project)
    shutil.copytree(ROOT / "tests", project / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    env = {key: value for key, value in os.environ.items() if key not in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT")}

    sync = subprocess.run(["uv", "sync", *sync_args], cwd=project, env=env, capture_output=True, text=True, timeout=600)
    assert sync.returncode == 0, sync.stdout + sync.stderr

    python = project / ".venv" / "bin" / "python"
    tests = subprocess.run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", *BASELINE], cwd=project, env=env, capture_output=True, text=True, timeout=600)
    assert tests.returncode == 0, tests.stdout + tests.stderr
