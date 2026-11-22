# -*- coding: utf-8 -*-
"""Cross-channel backends.

A backend here is an upstream runtime that serves MULTIPLE channels
(OpenCLI covers reddit and twitter through the Agent Reach browser).
"""

from .opencli import OPENCLI_EXTENSION_URL, opencli_call, opencli_items  # noqa: F401
