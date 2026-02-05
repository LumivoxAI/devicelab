from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/microphone_capture.py",
        "examples/file_capture.py",
        "examples/speaker_playback.py",
        "examples/recording.py",
        "examples/record_playback_gui.py",
        "tools/record_test_dataset.py",
    ],
)
def test_script_help_does_not_access_gstreamer_or_hardware(relative_path: str) -> None:
    script = Path(__file__).parents[1] / relative_path

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
