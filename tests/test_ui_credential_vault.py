"""Offline KWallet behavior, isolated from the Qt-free auth holder."""
import pytest

from covearchive.github.token import VaultError
from covearchive.ui.credential_vault import KWalletVault
from test_github_token import TOKEN

class DBus:
    def __init__(self, locked=False, broken=False):
        self.locked, self.broken, self.value, self.calls = locked, broken, None, []
    def isValid(self): return not self.broken
    def callWithArgumentList(self,mode,method,args):
        from PySide6.QtDBus import QDBus
        assert mode==QDBus.Block
        return self.call(method,*args)
    def call(self, method, *args):
        self.calls.append((method, args))
        if self.broken: raise RuntimeError('timeout')
        if method=='removeEntry': self.value=None
        return {'networkWallet': 'kdewallet', 'open': -1 if self.locked else 1,
                'hasFolder': True, 'hasEntry': True, 'createFolder': True, 'readPassword': self.value or '',
                'writePassword': 0, 'removeEntry': 0, 'close': 0}.get(method)

def test_kwallet_fake():
    bus = DBus(); vault = KWalletVault(interface=bus)
    assert vault.load() is None
    vault.save(TOKEN); assert any(m == 'writePassword' for m, _ in bus.calls)
    bus.value = TOKEN; assert vault.load() == TOKEN
    vault.delete(); assert any(m == 'removeEntry' for m, _ in bus.calls)
    for fake in (DBus(locked=True), DBus(broken=True)):
        with pytest.raises(VaultError): KWalletVault(interface=fake).load()

def test_kwallet_missing_and_close_failure():
    class Missing(DBus):
        def call(self, method, *args):
            if method == 'hasEntry': return False
            if method == 'close': raise RuntimeError('timeout')
            return super().call(method, *args)
    bus = Missing(); vault = KWalletVault(interface=bus)
    vault.delete()
    assert not any(m == 'removeEntry' for m, _ in bus.calls)
    bus.value = TOKEN
    assert vault.load() == TOKEN

@pytest.mark.parametrize('error',[False,True])
def test_kwallet_native_open_checks_last_error(monkeypatch,error):
    from types import SimpleNamespace as NS
    from PySide6.QtCore import QMetaObject
    class Interface(DBus):
        def lastError(self): return NS(isValid=lambda:error)
    bus=Interface(); vault=KWalletVault(interface=bus); vault._native=True
    monkeypatch.setattr(vault,'_connect',lambda:bus)
    monkeypatch.setattr(QMetaObject,'invokeMethod',lambda *args:1)
    if error:
        with pytest.raises(VaultError): vault.load()
        assert not any(method=='readPassword' for method,_ in bus.calls)
    else: assert vault.load() is None

class Reply:
    def __init__(self,value,valid=True): self.data,self.valid=value,valid
    def isValid(self): return self.valid
    def value(self): return self.data

class VoidReply:
    def isValid(self): return True
    def value(self): raise AssertionError('QDBusReply<void> has no value')

class Bus:
    def __init__(self,registered=(),activatable=(),connected=True,valid=True):
        self.registered,self.activatable,self.connected,self.valid=set(registered),list(activatable),connected,valid; self.started=[]
    def isConnected(self): return self.connected
    def interface(self): return None if self.valid is None else self
    def registeredServiceNames(self): return Reply(list(self.registered),self.valid)
    def activatableServiceNames(self): return Reply(self.activatable,self.valid)
    def isServiceRegistered(self,name): return Reply(name in self.registered,self.valid)
    def startService(self,name): self.started.append(name); self.registered.add(name); return VoidReply()

@pytest.mark.parametrize('connected,valid,absent',[(True,True,True),(False,True,False),(True,False,False),(True,None,False)])
def test_platform_only_discards_confirmed_absence(monkeypatch,connected,valid,absent):
    import covearchive.ui.credential_vault as module
    monkeypatch.setattr(module.sys,'platform','linux')
    monkeypatch.setattr(module,'_session_bus',lambda:Bus(connected=connected,valid=valid))
    assert (module.platform_vault() is None)==absent
    monkeypatch.setattr(module,'_session_bus',lambda:(_ for _ in ()).throw(ImportError()))
    assert module.platform_vault() is None

@pytest.mark.parametrize('name',['org.kde.kwalletd6','org.kde.kwalletd5'])
@pytest.mark.parametrize('running',[False,True])
def test_lazy_kwallet_activates_before_interface_and_retries_clear(monkeypatch,running,name):
    from covearchive.github.token import GitHubAuth
    from PySide6.QtCore import QMetaObject
    bus=Bus((name,) if running else (),[name],connected=False)
    stored=DBus(); stored.value=TOKEN; constructed=[]
    def interface(service,connection):
        assert service in connection.registered; constructed.append(service)
        stored.setTimeout=lambda n:None
        return stored
    vault=KWalletVault(bus_factory=lambda:bus,interface_factory=interface)
    auth=GitHubAuth(vault); assert 'Any saved token is not in use this session' in auth.load()
    assert 'Any saved token was not removed' in auth.clear() and stored.value==TOKEN
    assert auth.save(TOKEN) and auth.status().state=='SESSION'
    bus.connected=True; monkeypatch.setattr(QMetaObject,'invokeMethod',lambda *a:1)
    assert auth.clear() is None and constructed==[name]
    assert bus.started==([] if running else [name]) and stored.value is None
    restart=GitHubAuth(vault); assert restart.load() is None and restart.token() is None

@pytest.mark.parametrize('failure',['exception','timeout','invalid'])
def test_unknown_presence_keeps_lazy_vault(monkeypatch,failure):
    import covearchive.ui.credential_vault as module
    class Unknown(Bus):
        def registeredServiceNames(self):
            if failure=='exception': raise RuntimeError('Service query failed')
            if failure=='timeout': raise TimeoutError('Service query timed out')
            return Reply([],False)
    bus=Unknown(); monkeypatch.setattr(module.sys,'platform','linux')
    monkeypatch.setattr(module,'_session_bus',lambda:bus)
    assert module.kwallet_services(bus) is None
    assert isinstance(module.platform_vault(),KWalletVault)

@pytest.mark.parametrize('reply',[Reply(None,False)])
def test_failed_activation_cannot_claim_token_cleared(reply):
    from covearchive.github.token import GitHubAuth
    class FailedStart(Bus):
        def startService(self,name): return reply
    def interface(*args): pytest.fail('Interface constructed before activation succeeded')
    vault=KWalletVault(bus_factory=lambda:FailedStart(activatable=['org.kde.kwalletd6']),interface_factory=interface)
    with pytest.raises(VaultError): vault.delete()
    auth=GitHubAuth(vault); problem=auth.clear()
    assert 'Any saved token was not removed; try Clear Token again.' in problem
    assert auth.status().problem==problem and 'cleared' not in problem.lower()

@pytest.mark.parametrize('registered,activatable,expected',[
    (['org.kde.kwalletd6','org.kde.kwalletd5'],[],'org.kde.kwalletd6'),
    (['org.kde.kwalletd5'],[],'org.kde.kwalletd5'),
    ([],['org.kde.kwalletd6'],'org.kde.kwalletd6'),
])
def test_service_preference_and_activation_order(monkeypatch,registered,activatable,expected):
    from PySide6.QtCore import QMetaObject
    bus=Bus(registered,activatable); constructed=[]; stored=DBus()
    def interface(name,connection):
        assert name in connection.registered
        if not registered: assert connection.started==[expected]
        constructed.append(name); stored.setTimeout=lambda n:None
        return stored
    monkeypatch.setattr(QMetaObject,'invokeMethod',lambda *a:1)
    vault=KWalletVault(bus_factory=lambda:bus,interface_factory=interface)
    assert vault.load() is None and constructed==[expected]
    assert bus.started==([] if registered else [expected])
