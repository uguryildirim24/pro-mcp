"""Path policy: every path a tool touches must resolve inside an allowed root
and must not look like a secret."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# Matched against every path component (and the file name) of the resolved path.
DENY_NAMES = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials*",
    "secrets*",
    "*.keychain*",
    ".ssh",
    ".gnupg",
    ".aws",
]

# Directories we never descend into when listing or searching.
SKIP_DIRS = {".git", "node_modules", "target", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".target"}


class PolicyError(ValueError):
    """A path was outside the roots or matched the deny list."""


def is_denied_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in DENY_NAMES)


@dataclass
class Sandbox:
    roots: list[Path] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Sandbox":
        raw = os.environ.get("PRO_MCP_ROOTS", "~/projects")
        roots = [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]
        return cls(roots=roots)

    def resolve(self, path: str) -> Path:
        """Resolve a user-supplied path (absolute, ~-relative, or relative to the
        first root) and enforce the policy. Symlinks are resolved first, so a link
        pointing outside the roots is rejected."""
        if not self.roots:
            raise PolicyError("no roots configured")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        real = p.resolve()
        root = next((r for r in self.roots if real == r or real.is_relative_to(r)), None)
        if root is None:
            raise PolicyError(f"{path} is outside the allowed roots: {', '.join(map(str, self.roots))}")
        for part in real.relative_to(root).parts:
            if is_denied_name(part):
                raise PolicyError(f"{path} matches the secrets deny list ({part})")
        return real

    def display(self, real: Path) -> str:
        home = Path.home()
        return "~/" + str(real.relative_to(home)) if real.is_relative_to(home) else str(real)
