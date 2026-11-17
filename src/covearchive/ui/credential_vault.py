"""Platform vault selection and the optional KWallet D-Bus adapter.

KWallet uses only simple D-Bus types. Secret Service requires struct marshalling
that PySide6 cannot express cleanly, so it is deliberately not implemented.
Qt is imported lazily, only when using the Linux vault.
"""
import sys

from covearchive.github.token import VaultError, WindowsCredentialVault, valid_token


SERVICES = ('org.kde.kwalletd6', 'org.kde.kwalletd5')


def _session_bus():
    from PySide6.QtDBus import QDBusConnection
    return QDBusConnection.sessionBus()


def kwallet_services(bus):
    """Registered first, then activatable; () means absent, None means unknown."""
    try:
        if not bus.isConnected() or bus.interface() is None: return None
        manager = bus.interface()
        if hasattr(manager,'setTimeout'): manager.setTimeout(1500)
        replies = (manager.registeredServiceNames(),manager.activatableServiceNames())
        if not all(reply.isValid() for reply in replies): return None
        registered, activatable = (reply.value() for reply in replies)
        if not all(isinstance(names,list) and all(isinstance(n,str) for n in names) for names in (registered,activatable)): return None
        return tuple(s for s in SERVICES if s in registered)+tuple(s for s in SERVICES if s in activatable and s not in registered)
    except Exception: return None


def _interface(service,bus):
    from PySide6.QtDBus import QDBusInterface
    return QDBusInterface(service,'/modules/'+service.rsplit('.',1)[1],'org.kde.KWallet',bus)


class KWalletVault:
    label = 'KWallet'
    _folder, _key, _app = 'CoveArchive', 'github-token', 'CoveArchive'

    def __init__(self, *, interface=None, bus_factory=None, interface_factory=None):
        self._interface, self._native = interface, interface is None
        self._bus_factory, self._interface_factory = bus_factory, interface_factory
        if interface is not None and not interface.isValid(): raise self._failure()

    def _connect(self):
        try:
            bus = (self._bus_factory or _session_bus)(); services = kwallet_services(bus)
            if not services: raise self._failure()
            manager = bus.interface()
            if hasattr(manager,'setTimeout'): manager.setTimeout(1500)
            for service in services:
                reply = manager.isServiceRegistered(service)
                if not reply.isValid() or type(reply.value()) is not bool: raise self._failure()
                if not reply.value():
                    reply = manager.startService(service)
                    if not reply.isValid(): continue
                candidate = (self._interface_factory or _interface)(service,bus)
                candidate.setTimeout(1500)
                if candidate.isValid(): return candidate
        except Exception: raise self._failure() from None
        raise self._failure()

    def _failure(self):
        return VaultError('KWallet is locked or unavailable. Open and unlock KWallet, then try again.')

    def _call(self, method, *args):
        try:
            if self._native:
                from PySide6.QtDBus import QDBus
                response = self._interface.callWithArgumentList(QDBus.Block,method,list(args))
            else: response = self._interface.call(method, *args)
            if hasattr(response, 'errorName'):
                if response.errorName(): raise self._failure()
                values = response.arguments()
                if not values: raise self._failure()
                return values[0]
            return response
        except Exception:
            raise self._failure() from None

    def _open(self):
        name = self._call('networkWallet')
        if self._native:
            # invokeMethod supplies the exact x (qlonglong) wId required by
            # KWallet. Python ints through QDBusInterface.call become i; the
            # overloaded QDBusArgument << int is ambiguous in PySide6 6.11.
            from PySide6.QtCore import QMetaObject, Qt, Q_ARG, Q_RETURN_ARG
            try:
                handle = QMetaObject.invokeMethod(self._interface, 'open', Qt.DirectConnection,
                    Q_RETURN_ARG(int), Q_ARG(str, name), Q_ARG('qlonglong', 0), Q_ARG(str, self._app))
                last_error=getattr(self._interface,'lastError',None)
                if callable(last_error) and last_error().isValid(): raise self._failure()
            except Exception: raise self._failure() from None
        else: handle = self._call('open', name, 0, self._app)
        if type(handle) is not int or handle < 0: raise self._failure()
        return handle

    def _operation(self, method, token=None):
        if self._native: self._interface = self._connect()
        try: handle = self._open()
        except Exception:
            if self._native: self._interface = None
            raise
        try:
            if method == 'removeEntry' and not self._call('hasEntry', handle, self._folder, self._key, self._app):
                return
            if method == 'writePassword' and not self._call('hasFolder', handle, self._folder, self._app):
                if not self._call('createFolder', handle, self._folder, self._app): raise self._failure()
            args = (handle, self._folder, self._key)
            if token is not None: args += (token,)
            value = self._call(method, *args, self._app)
            if method != 'readPassword' and value != 0: raise self._failure()
            if method == 'readPassword':
                if not isinstance(value, str): raise self._failure()
                return value or None
        finally:
            try: self._call('close', handle, False, self._app)
            except VaultError: pass
            if self._native: self._interface = None

    def load(self): return self._operation('readPassword')
    def save(self, token):
        if not valid_token(token): raise VaultError('Enter a valid GitHub token.')
        self._operation('writePassword', token)
    def delete(self): self._operation('removeEntry')


def platform_vault():
    if sys.platform == 'win32': return WindowsCredentialVault()
    if sys.platform.startswith('linux'):
        try:
            if kwallet_services(_session_bus()) == (): return None
        except ImportError: return None
        except Exception: pass
        return KWalletVault()
    return None
