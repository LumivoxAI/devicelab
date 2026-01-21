from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name",
    ["microphone_capture.py", "file_capture.py", "speaker_playback.py", "recording.py"],
)
def test_example_help_does_not_access_gstreamer_or_hardware(name: str) -> None:
    example = Path(__file__).parents[1] / "examples" / name

    result = subprocess.run(
        [sys.executable, str(example), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
