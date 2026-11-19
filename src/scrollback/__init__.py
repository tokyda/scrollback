"""Scrollback: an LLM data agent over your own TikTok watch history."""

import logging

__version__ = "0.1.0"

# Library convention: callers (the CLI, tests) decide how warnings are shown.
logging.getLogger(__name__).addHandler(logging.NullHandler())
