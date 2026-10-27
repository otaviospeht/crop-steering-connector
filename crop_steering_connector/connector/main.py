"""Process entry point: logging to the Log tab, the connector, then idle until the user restarts the app."""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Callable

from .runtime import Connector
from .transport import UrllibTransport

DATA_DIR = Path("/data")


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S"))
    logger = logging.getLogger("crop_steering_connector")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def idle_forever() -> None:
    threading.Event().wait()


def main(
    data_dir: Path = DATA_DIR,
    connector_factory: Callable[..., Connector] = Connector,
    idle: Callable[[], None] = idle_forever,
) -> int:
    os.umask(0o077)
    logger = configure_logging()
    connector = connector_factory(
        data_dir=data_dir,
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN"),
        transport=UrllibTransport(),
        log=logger,
    )
    connector.run()
    # Never exit: an exit would make the watchdog restart the app and repeat the last line in a loop.
    idle()
    return 0
