"""
# Ediscord
Cog package for the Prowl Discord bot.

Contains:
- Shared utilities and variables
- Helper functions for the main bot
- Builder utilities for embeds, buttons, links, and modals

**Copyright (C) 2025 th3_t1sm. (GPL v3)**
"""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import order matters: `variables` has no Ediscord deps, and both
# `builders` and `utils` import it. Import it first to avoid circular imports.
from Ediscord import variables

__version__ = variables.__version__
__author__ = "th3_t1sm"

from Ediscord import db
from Ediscord import utils
from Ediscord import builders
from Ediscord.builders import (
    EmbedBuilder,
    ButtonBuilder,
    ModalBuilder,
    LinkBuilder,
    quick_embed,
    success_embed,
    error_embed,
    info_embed,
)
