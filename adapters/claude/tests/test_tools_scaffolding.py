import os
import sys
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER_DIR = os.path.dirname(_HERE)
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

def test_tools_package_imports():
    from tools.common import CallCallable, CallRawCallable
    assert CallCallable is not None
    assert CallRawCallable is not None
