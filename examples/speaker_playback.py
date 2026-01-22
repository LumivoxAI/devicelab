"""Play a generated mono tone through a selected speaker."""

from __future__ import annotations

import argparse

import numpy as np
from lumivox_core.logger import LoggingConfig, get_logger, configure_logging

from lumivox_devicelab import AudioFormat, SpeakerDeviceDiscovery, SpeakerPlaybackPipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("device_id", nargs="?", help="stable node.name from speaker discovery")
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--frequency", type=float, default=440.0)
    args = parser.parse_args()

    configure_logging(LoggingConfig(application="devicelab-speaker-example"))
    logger = get_logger(example="speaker_playback")
    snapshot = SpeakerDeviceDiscovery(logger=logger).snapshot()
    if args.device_id is None:
        for device in snapshot.devices:
            print(f"{device.id}: {device.name}")
        raise SystemExit("Pass one of the listed device IDs to play a tone")

    sample_rate = 16_000
    frame_count = int(sample_rate * args.duration)
    time = np.arange(frame_count, dtype=np.float64) / sample_rate
    samples = (np.sin(2 * np.pi * args.frequency * time) * 8_000).astype("<i2")
    pipeline = SpeakerPlaybackPipeline(
        logger=logger,
        audio_format=AudioFormat(sample_rate=sample_rate, channels=1),
        device_id=args.device_id,
    )
    pipeline.start()
    pipeline.submit(samples)
    pipeline.stop()


if __name__ == "__main__":
    main()
