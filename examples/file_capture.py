"""Print metadata while replaying a WAV or FLAC file."""

from __future__ import annotations

import argparse

from lumivox_core.logger import LoggingConfig, get_logger, configure_logging

from lumivox_devicelab import (
    AudioFormat,
    CapturedChunk,
    CaptureHandler,
    FileReplayMode,
    FileCapturePipeline,
)


class PrintHandler(CaptureHandler):
    def on_chunk(self, chunk: CapturedChunk) -> None:
        print(f"frames={len(chunk.samples)} running_time_ns={chunk.running_time_ns}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="input .wav or .flac file")
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--realtime", action="store_true", help="pace replay against the pipeline clock")
    args = parser.parse_args()

    configure_logging(LoggingConfig(application="devicelab-file-example"))
    logger = get_logger(example="file_capture")
    replay_mode = FileReplayMode.REALTIME if args.realtime else FileReplayMode.AS_FAST_AS_POSSIBLE
    pipeline = FileCapturePipeline(
        logger=logger,
        handler=PrintHandler(),
        audio_format=AudioFormat(sample_rate=args.sample_rate, channels=1),
        path=args.path,
        replay_mode=replay_mode,
    )
    pipeline.start()
    pipeline.wait()


if __name__ == "__main__":
    main()
