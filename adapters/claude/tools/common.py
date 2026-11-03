from __future__ import annotations

from typing import Any, Callable, Dict, Optional

# Protocol types for the execution client wrappers
CallCallable = Callable[[str, Dict[str, Any]], str]
CallRawCallable = Callable[..., Dict[str, Any]]
