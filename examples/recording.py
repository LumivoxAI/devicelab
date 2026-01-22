"""Capture a selected microphone to WAV or FLAC until interrupted."""

from __future__ import annotations

import argparse
from pathlib import Path

from lumivox_core.logger import LoggingConfig, get_logger, configure_logging

from lumivox_devicelab import AudioFormat, CapturedChunk, CaptureHandler, MicrophoneCapturePipeline


class FrameCounter(CaptureHandler):
    def __init__(self) -> None:
        self.frames = 0

    def on_chunk(self, chunk: CapturedChunk) -> None:
        self.frames += len(chunk.samples)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("device_id", help="stable microphone node.name")
    parser.add_argument("output", type=Path, help="output .wav or .flac file")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    configure_logging(LoggingConfig(application="devicelab-recording-example"))
    logger = get_logger(example="recording")
    handler = FrameCounter()
    pipeline = MicrophoneCapturePipeline(
        logger=logger,
        handler=handler,
        audio_format=AudioFormat(sample_rate=16_000, channels=1),
        device_id=args.device_id,
        record_to=args.output,
        overwrite=args.overwrite,
    )
    try:
        pipeline.start()
        pipeline.wait()
    except KeyboardInterrupt:
        pipeline.stop()
    print(f"recorded {handler.frames} frames")


if __name__ == "__main__":
    main()
