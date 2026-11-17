"""Optional GitHub credentials; secrets stay in memory or the OS vault."""

from dataclasses import dataclass
import re
import threading


def valid_token(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{20,255}', value) is not None


@dataclass(frozen=True)
class RateLimit:
    limit: int | None
    remaining: int
    reset: int
    authenticated: bool
    observed_at: float


@dataclass(frozen=True)
class AuthStatus:
    state: str
    label: str
    problem: str | None
    rate: RateLimit | None


class VaultError(Exception):
    """Actionable, secret-free vault failure."""


class StaticAuth:
    def __init__(self, token):
        self._token = token if valid_token(token) else None
        self.rate = None
        self.rejected = False

    def token(self): return self._token
    def observe(self, rate, token): self.rate = rate
    def reject(self, token):
        if token is not None and token == self._token:
            self._token = None; self.rejected = True; self.rate = None


class GitHubAuth:
    """Thread-safe application credential holder; status never includes secrets."""
    def __init__(self, vault):
        self._vault, self._token = vault, None
        self._lock = threading.RLock()
        self._vault_lock = threading.Lock()
        self._state, self._problem, self._rate = 'NONE', None, None
        self._serial = 0

    @property
    def rate(self):
        with self._lock: return self._rate

    def load(self):
        with self._lock: serial = self._serial
        token, problem = None, None
        try:
            with self._vault_lock:
                token = self._vault.load() if self._vault else None
            if token is not None and not valid_token(token): raise VaultError('Replace the invalid GitHub credential in settings.')
        except VaultError as exc:
            token, problem = None, str(exc)
            if self._vault.label == 'KWallet': problem += ' Any saved token is not in use this session; unlock KWallet and restart CoveArchive.'
        with self._lock:
            if serial == self._serial:
                self._token, self._state, self._problem, self._rate = token, 'SAVED' if token else 'NONE', problem, None
            return self._problem

    def token(self):
        with self._lock: return None if self._state == 'REJECTED' else self._token

    def save(self, token):
        with self._lock:
            if not valid_token(token): return 'Enter a GitHub token of 20-255 letters, digits, underscores or hyphens.'
            self._token, self._state, self._rate, self._problem = token, 'SESSION', None, None
            self._serial += 1; serial = self._serial
        problem = None
        try:
            if self._vault is None: raise VaultError('No supported credential vault is available.')
            with self._vault_lock: self._vault.save(token)
        except VaultError as exc:
            problem = str(exc) + ' The token is used for this session only; fix storage and paste it again to keep it.'
        with self._lock:
            if serial == self._serial and self._state == 'SESSION':
                self._problem = problem
                if problem is None: self._state = 'SAVED'
            return self._problem

    def clear(self):
        with self._lock:
            self._token, self._state, self._problem, self._rate = None, 'NONE', None, None
            self._serial += 1; serial = self._serial
        problem = None
        try:
            with self._vault_lock:
                if self._vault: self._vault.delete()
        except VaultError as exc:
            problem = str(exc) + ' Any saved token was not removed; try Clear Token again.'
        with self._lock:
            if serial == self._serial: self._problem = problem
            return self._problem

    def observe(self, rate, token):
        with self._lock:
            if token == self.token(): self._rate = rate

    def reject(self, token):
        with self._lock:
            if token is not None and token == self._token and self._state != 'REJECTED':
                self._state, self._rate = 'REJECTED', None
                self._serial += 1

    def status(self):
        with self._lock:
            return AuthStatus(self._state, self._vault.label if self._vault else 'Session only',
                              self._problem, self._rate)


class WindowsCredentialVault:
    label = 'Windows Credential Manager'

    def __init__(self, target='CoveArchive/github-token'):
        import ctypes as c
        from ctypes import wintypes as w
        class Credential(c.Structure):
            _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR),
                        ('Comment', w.LPWSTR), ('LastWritten', w.FILETIME),
                        ('CredentialBlobSize', w.DWORD), ('CredentialBlob', c.POINTER(c.c_ubyte)),
                        ('Persist', w.DWORD), ('AttributeCount', w.DWORD),
                        ('Attributes', c.c_void_p), ('TargetAlias', w.LPWSTR), ('UserName', w.LPWSTR)]
        self._c, self._Credential, self._target = c, Credential, target
        self._api = c.WinDLL('advapi32', use_last_error=True)
        pointer = c.POINTER(Credential)
        self._api.CredWriteW.argtypes = [pointer, w.DWORD]
        self._api.CredWriteW.restype = w.BOOL
        self._api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(pointer)]
        self._api.CredReadW.restype = w.BOOL
        self._api.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
        self._api.CredDeleteW.restype = w.BOOL
        self._api.CredFree.argtypes = [c.c_void_p]
        self._api.CredFree.restype = None

    def _failure(self):
        return VaultError('Windows Credential Manager is unavailable. Unlock your Windows session and try again.')

    def load(self):
        c = self._c; pointer = c.POINTER(self._Credential)()
        if not self._api.CredReadW(self._target, 1, 0, c.byref(pointer)):
            if c.get_last_error() == 1168: return None
            raise self._failure()
        try:
            try: return c.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize).decode('utf-8')
            except UnicodeError: raise self._failure() from None
        finally: self._api.CredFree(pointer)

    def save(self, token):
        if not valid_token(token): raise VaultError('Enter a valid GitHub token.')
        c = self._c; blob = (c.c_ubyte * len(token)).from_buffer_copy(token.encode('utf-8'))
        credential = self._Credential(Type=1, TargetName=self._target, CredentialBlobSize=len(blob),
                                      CredentialBlob=blob, Persist=2, UserName='CoveArchive')
        if not self._api.CredWriteW(c.byref(credential), 0): raise self._failure()

    def delete(self):
        if not self._api.CredDeleteW(self._target, 1, 0) and self._c.get_last_error() != 1168:
            raise self._failure()
