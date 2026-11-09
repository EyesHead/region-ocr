"""Windows current-user secret protection; no storage or network at import."""
from __future__ import annotations

import ctypes
import os


def protect_secret(value, *, entropy, description, decrypt=False):
    if os.name != 'nt':
        raise ValueError('Постоянное хранение токена доступно только через Windows DPAPI; используйте переменные окружения')
    if not isinstance(value, bytes) or not value or not isinstance(entropy, bytes) or not entropy:
        raise ValueError('Некорректные данные защищённой настройки')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    def blob(data):
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return Blob(len(data), buffer), buffer

    incoming, incoming_buffer = blob(value)
    extra, extra_buffer = blob(entropy)
    outgoing = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        success = function(ctypes.byref(incoming), None, ctypes.byref(extra), None, None, 1, ctypes.byref(outgoing))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN; never CRYPTPROTECT_LOCAL_MACHINE.
        success = function(ctypes.byref(incoming), description, ctypes.byref(extra), None, None, 1, ctypes.byref(outgoing))
    if not success:
        raise ValueError('Защищённая настройка недоступна текущему пользователю Windows' if decrypt else
                         'Не удалось защитить токен средствами Windows')
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.pbData, wintypes.HLOCAL))
