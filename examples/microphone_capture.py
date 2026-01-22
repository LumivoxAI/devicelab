"""Print metadata for audio captured from a discovered microphone."""

from __future__ import annotations

import argparse

from lumivox_core.logger import LoggingConfig, get_logger, configure_logging

from lumivox_devicelab import (
    AudioFormat,
    CapturedChunk,
    CaptureHandler,
    MicrophoneCapturePipeline,
    MicrophoneDeviceDiscovery,
)


class PrintHandler(CaptureHandler):
    def on_chunk(self, chunk: CapturedChunk) -> None:
        print(
            f"generation={chunk.generation} frames={len(chunk.samples)} "
            f"captured_at_ns={chunk.captured_at_ns} discontinuity={chunk.discontinuity}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("device_id", nargs="?", help="stable node.name from microphone discovery")
    args = parser.parse_args()

    configure_logging(LoggingConfig(application="devicelab-microphone-example"))
    logger = get_logger(example="microphone_capture")
    snapshot = MicrophoneDeviceDiscovery(logger=logger).snapshot()
    if args.device_id is None:
        for device in snapshot.devices:
            marker = " (default)" if device.is_default else ""
            print(f"{device.id}: {device.name}{marker}")
        if not snapshot.devices:
            raise SystemExit("No microphones discovered")
        raise SystemExit("Pass one of the listed device IDs to start capture")
    if all(device.id != args.device_id for device in snapshot.devices):
        raise SystemExit(f"Microphone {args.device_id!r} was not found")

    pipeline = MicrophoneCapturePipeline(
        logger=logger,
        handler=PrintHandler(),
        audio_format=AudioFormat(sample_rate=16_000, channels=1),
        device_id=args.device_id,
    )
    try:
        pipeline.start()
        pipeline.wait()
    except KeyboardInterrupt:
        pipeline.stop()


if __name__ == "__main__":
    main()
