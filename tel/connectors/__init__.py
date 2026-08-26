"""Outside-world feeds that give AI activity an outcome.

Every connector is read-only, runs a local CLI that is already authenticated
(`gh`, `eas`, `railway`), and writes into the same normalized database. None
of them send anything anywhere.
"""
from . import github, eas  # noqa: F401

REGISTRY = {
    "github": github.collect,
    "eas": eas.collect,
}
