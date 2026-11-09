"""Windows extended paths for long document packages, portable public names."""
import os
from pathlib import Path


def _get_long_windows_name(value):
    """Expand a DOS alias without resolving symlinks/junctions to their targets."""
    import ctypes
    from ctypes import wintypes

    function = ctypes.WinDLL('kernel32', use_last_error=True).GetLongPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    required = function(value, None, 0)
    if not required:
        return None
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = function(value, buffer, len(buffer))
    return buffer.value if written and written < len(buffer) else None


def _strip_extended_prefix(text):
    if text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + text[8:]
    if text.startswith('\\\\?\\'):
        return text[4:]
    return text


def _expand_windows_aliases(text):
    candidate = Path(text)
    missing = []
    while True:
        # The extended form also permits expansion of existing paths >260 chars.
        value = str(native_path(candidate)) if candidate.is_absolute() else str(candidate)
        expanded = _get_long_windows_name(value)
        if expanded is not None:
            return str(Path(_strip_extended_prefix(expanded)).joinpath(*reversed(missing)))
        parent = candidate.parent
        if parent == candidate or not candidate.name:
            return text
        # A new output file/folder may not exist yet. Expand its existing parent,
        # then preserve the exact new names; never invent or rename a component.
        missing.append(candidate.name)
        candidate = parent


def normal_path(value):
    text = str(value)
    if os.name == 'nt':
        text = _strip_extended_prefix(text)
        if '~' in text:
            text = _expand_windows_aliases(text)
    return Path(text)


def native_path(value):
    path = Path(value)
    if os.name != 'nt':
        return path
    text = str(path.absolute())
    if text.startswith('\\\\?\\'):
        return path
    if text.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + text[2:])
    return Path('\\\\?\\' + text)
