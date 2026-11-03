from pathlib import Path
import subprocess


def test_candle_panel_request_isolation_visibility_and_teardown():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node','--test',str(root/'tests/grid_candle_panel.test.mjs')],
                            cwd=root,capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
