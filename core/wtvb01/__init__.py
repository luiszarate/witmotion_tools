"""Device library for the WitMotion WTVB01-BT50.

Everything here is transport- and application-agnostic: the frame protocol,
the register map, capture modes, serial acquisition and CSV writing. The
desktop monitor (``mac/``) and the Raspberry Pi logger (``pi/``) both build on
it, so protocol knowledge lives in exactly one place.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
