# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging


def get_logger(name: str, debug: bool):
    # Get the logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Create console handler
    console_handler = logging.StreamHandler()
    if debug:
        console_handler.setLevel(logging.DEBUG)
    else:
        console_handler.setLevel(logging.INFO)

    # Set formatter
    formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")
    console_handler.setFormatter(formatter)

    # Add handlers
    logger.addHandler(console_handler)

    # Return
    return logger
