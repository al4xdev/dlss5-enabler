import logging
from pathlib import Path

from rich.logging import RichHandler

from dlss5_enabler.platform import get_platform_adapter


class _LogState:
    initialized: bool = False


def get_log_dir() -> Path:
    return get_platform_adapter().get_log_dir()


def setup_logger(verbose: bool = False, custom_log_file: Path | str | None = None) -> logging.Logger:
    logger = logging.getLogger("dlss5_enabler")

    if _LogState.initialized:
        if verbose:
            logger.setLevel(logging.DEBUG)
            for handler in logger.handlers:
                handler.setLevel(logging.DEBUG)
        return logger

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    console_handler = RichHandler(
        rich_tracebacks=True,
        show_time=False,
        show_path=verbose,
        markup=True,
    )
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console_handler)

    log_file = Path(custom_log_file) if custom_log_file else get_log_dir() / "dlss5-enabler.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    _LogState.initialized = True
    return logger


def get_logger(name: str = "dlss5_enabler") -> logging.Logger:
    return logging.getLogger(f"dlss5_enabler.{name}")
