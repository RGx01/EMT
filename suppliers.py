"""
suppliers.py — the supplier registry, in one place.

Mirrors the client WIZ_SUPPLIERS registry. The engine's supplier gate decides whether
an API-backed mode is permitted at all; the import gate (import_gate.py) decides
whether a backup's supplier is one this build understands. Adding a future
Kraken-platform supplier = add its key here (+ eventually a profile).

Its own module so the import gate can read it without importing the engine.
"""

API_CAPABLE = frozenset({"octopus"})

# The deliberate local-metering-only answer ("My supplier isn't listed").
LOCAL_ONLY = "not-listed"
