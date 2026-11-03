"""Exercise actual browser queue module without network or a venue."""
from pathlib import Path
import subprocess


def test_preview_queue_concurrency_visibility_and_cleanup():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', '--test', str(root/'tests/grid_preview_queue.test.mjs')],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_approved_mode_and_protection_defaults_use_one_exact_grant():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', '--test', str(root/'tests/grid_qualification.test.mjs')],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
