"""A minimal environment for programs the server starts (kicad-cli, the docker client).

The server process can hold secrets, for example a model API key and the launch token. A child
program that parses uploaded files, or that only talks to a local daemon, has no use for them, so
it is started with an allow-list of variables instead of a copy of the server's environment.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

#: Needed to find programs, the home folder, temp folders, the locale and (on Windows) the system.
BASE_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TMPDIR", "TEMP", "TMP", "TZ", "SHELL",
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "LD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMPUTERNAME",
})
BASE_PREFIXES = ("LC_", "XDG_", "FONTCONFIG_")


def child_environment(
    extra_names: Iterable[str] = (),
    extra_prefixes: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The variables of ``environ`` (default: the process environment) that a child may keep.

    Matching ignores case on Windows only, where variable names are case-insensitive.
    """
    source = os.environ if environ is None else environ
    names = BASE_NAMES | {n.upper() for n in extra_names}
    prefixes = (*BASE_PREFIXES, *(p.upper() for p in extra_prefixes))
    return {
        key: value
        for key, value in source.items()
        if (key.upper() if os.name == "nt" else key) in names or (key.upper() if os.name == "nt" else key).startswith(prefixes)
    }
