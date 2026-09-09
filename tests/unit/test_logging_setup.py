from __future__ import annotations

import logging

from cognis.logging import setup_logging


def test_setup_logging_preserves_external_root_handlers() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    external = logging.NullHandler()
    root.addHandler(external)

    try:
        setup_logging()
        assert external in root.handlers

        configured_handlers = [
            handler
            for handler in root.handlers
            if getattr(handler, "_cognis_configured_handler", False)
        ]
        assert len(configured_handlers) == 1

        setup_logging()
        assert external in root.handlers
        assert (
            sum(
                bool(getattr(handler, "_cognis_configured_handler", False))
                for handler in root.handlers
            )
            == 1
        )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert root.handlers == original_handlers
    assert root.level == original_level
