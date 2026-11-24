"""Fail-closed Windows owner/DACL checks, with an injectable descriptor reader.

Only ctypes Windows security APIs are used. Importing this module on POSIX does
not load Windows libraries; tests inject the same normalized descriptor format.
"""
from functools import lru_cache
import os

ADMIN = "S-1-5-32-544"
SYSTEM = "S-1-5-18"
# Write/append/EA/attributes, delete child/object, WRITE_DAC/OWNER, generic
# write/all, maximum-allowed and system-security rights are mutation-capable.
MUTATION = 0x530D0156
ALLOW = {0, 5, 9, 11}
DENY = {1, 6, 10, 12}


class SecurityError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("Windows archive permissions could not be trusted.")


def check_path(path, *, reader=None):
    if reader is None and os.name != "nt": return
    try:
        value = (reader or _windows_descriptor)(path)
        if set(value) != {"owner", "user", "dacl"}: raise ValueError()
        if not all(isinstance(value[key], str) and value[key].startswith("S-") for key in ("owner", "user")): raise ValueError()
        trusted = {value["user"], ADMIN, SYSTEM}
        if value["owner"] not in trusted or value["dacl"] is None: raise SecurityError("unsafe_permissions")
        if not isinstance(value["dacl"], (list, tuple)) or len(value["dacl"]) > 4096: raise ValueError()
        for ace in value["dacl"]:
            if set(ace) != {"type", "flags", "mask", "sid"}: raise ValueError()
            if type(ace["type"]) is not int or type(ace["mask"]) is not int or not 0 <= ace["mask"] <= 0xffffffff: raise ValueError()
            if type(ace["flags"]) is not int or not 0 <= ace["flags"] <= 255: raise ValueError()
            if ace["type"] in DENY: continue
            if ace["type"] not in ALLOW or not isinstance(ace["sid"], str): raise ValueError()
            # These inherit-only SIDs are placeholders, not foreign principals
            # receiving access to this object. Descendant ACLs are checked too.
            if ace["sid"] in {"S-1-3-0", "S-1-3-1"} and ace["flags"] & 8: continue
            principal = value["owner"] if ace["sid"] in {"S-1-3-0", "S-1-3-4"} else ace["sid"]
            if ace["mask"] & MUTATION and principal not in trusted: raise SecurityError("unsafe_permissions")
    except SecurityError: raise
    except Exception: raise SecurityError("permissions_unavailable") from None


@lru_cache(maxsize=1)
def _apis():
    import ctypes as C
    from ctypes import wintypes as W
    api = C.WinDLL("advapi32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    ptr = C.c_void_p; pptr = C.POINTER(ptr)
    def bind(lib, name, args, result):
        function = getattr(lib, name); function.argtypes = args; function.restype = result
    bind(kernel, "GetCurrentProcess", [], W.HANDLE)
    bind(kernel, "CloseHandle", [W.HANDLE], W.BOOL)
    bind(kernel, "LocalFree", [ptr], ptr)
    bind(api, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)
    bind(api, "GetTokenInformation", [W.HANDLE, W.DWORD, ptr, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
    bind(api, "GetNamedSecurityInfoW", [W.LPWSTR, W.DWORD, W.DWORD, pptr, pptr, pptr, pptr, pptr], W.DWORD)
    bind(api, "GetAclInformation", [ptr, ptr, W.DWORD, W.DWORD], W.BOOL)
    bind(api, "GetAce", [ptr, W.DWORD, pptr], W.BOOL)
    bind(api, "IsValidSid", [ptr], W.BOOL)
    bind(api, "ConvertSidToStringSidW", [ptr, C.POINTER(W.LPWSTR)], W.BOOL)
    return api, kernel


def _sid_text(sid):
    import ctypes as C
    from ctypes import wintypes as W
    api, kernel = _apis(); text = W.LPWSTR()
    if not sid or not api.IsValidSid(sid) or not api.ConvertSidToStringSidW(sid, C.byref(text)):
        raise OSError("SID query failed")
    try: return text.value
    finally: kernel.LocalFree(C.cast(text, C.c_void_p))


@lru_cache(maxsize=1)
def _current_sid():
    import ctypes as C
    from ctypes import wintypes as W
    api, kernel = _apis(); token = W.HANDLE(); size = W.DWORD()
    if not api.OpenProcessToken(kernel.GetCurrentProcess(), 8, C.byref(token)): raise OSError("Token query failed")
    try:
        api.GetTokenInformation(token, 1, None, 0, C.byref(size))
        if not 0 < size.value <= 65536: raise OSError("Token size invalid")
        data = C.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, data, size, C.byref(size)): raise OSError("Token query failed")
        return _sid_text(C.cast(data, C.POINTER(C.c_void_p))[0])
    finally: kernel.CloseHandle(token)


def _windows_descriptor(path):
    import ctypes as C
    from ctypes import wintypes as W
    api, kernel = _apis()
    owner = C.c_void_p(); dacl = C.c_void_p(); descriptor = C.c_void_p()
    status = api.GetNamedSecurityInfoW(str(path), 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor))
    if status: raise OSError(status, "Security descriptor query failed")
    try:
        result = dict(owner=_sid_text(owner), user=_current_sid(), dacl=None)
        if not dacl: return result  # NULL DACL grants everyone full access.
        class AclSize(C.Structure):
            _fields_ = [("count", W.DWORD), ("used", W.DWORD), ("free", W.DWORD)]
        size = AclSize()
        if not api.GetAclInformation(dacl, C.byref(size), C.sizeof(size), 2) or size.count > 4096:
            raise OSError("DACL query failed")
        aces = []
        for index in range(size.count):
            pointer = C.c_void_p()
            if not api.GetAce(dacl, index, C.byref(pointer)) or not pointer: raise OSError("ACE query failed")
            address = pointer.value
            kind = C.c_ubyte.from_address(address).value
            flags = C.c_ubyte.from_address(address + 1).value
            length = C.c_ushort.from_address(address + 2).value
            if kind not in ALLOW | DENY or length < 16: raise OSError("Unsupported ACE")
            mask = C.c_uint32.from_address(address + 4).value; offset = 8
            if kind in {5, 6, 11, 12}:
                if length < 20: raise OSError("Invalid object ACE")
                object_flags = C.c_uint32.from_address(address + 8).value
                if object_flags & ~3: raise OSError("Unsupported object ACE")
                offset = 12 + (16 if object_flags & 1 else 0) + (16 if object_flags & 2 else 0)
            if length < offset + 8: raise OSError("Invalid ACE SID")
            subauthorities = C.c_ubyte.from_address(address + offset + 1).value
            if length < offset + 8 + 4 * subauthorities: raise OSError("Invalid ACE SID size")
            aces.append(dict(type=kind, flags=flags, mask=mask, sid=_sid_text(address + offset)))
        result["dacl"] = aces
        return result
    finally:
        if descriptor: kernel.LocalFree(descriptor)
