from pathlib import Path
import subprocess


def test_detail_refresh_keeps_ledger_independent_and_preserves_preview_guards():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', '--test', str(root/'tests/grid_detail_refresh.test.mjs')],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
