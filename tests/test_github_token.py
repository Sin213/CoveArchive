import sys
from uuid import uuid4

import pytest

from covearchive.github.token import (
    GitHubAuth, StaticAuth, RateLimit, VaultError, WindowsCredentialVault,
    valid_token,
)

TOKEN = 'ghp_' + 'A' * 36
OTHER = 'github_pat_' + 'B' * 40

class Vault:
    label = 'Test vault'
    def __init__(self): self.value = None; self.fail = False
    def load(self):
        if self.fail: raise VaultError('Unlock Test vault and try again.')
        return self.value
    def save(self, token):
        if self.fail: raise VaultError('Unlock Test vault and try again.')
        self.value = token
    def delete(self):
        if self.fail: raise VaultError('Unlock Test vault and try again.')
        self.value = None

@pytest.mark.parametrize('value', [None, '', 'a'*19, 'a'*256, TOKEN+'\r\nX: y', 'x y'*20])
def test_validation(value):
    assert not valid_token(value)

def test_auth_lifecycle_and_restart():
    vault = Vault(); auth = GitHubAuth(vault)
    assert auth.load() is None and auth.status().state == 'NONE'
    assert auth.save(TOKEN) is None and auth.status().state == 'SAVED'
    restarted = GitHubAuth(vault); restarted.load()
    assert restarted.token() == TOKEN
    auth.reject(OTHER); assert auth.token() == TOKEN
    auth.reject(TOKEN); assert auth.token() is None and auth.status().state == 'REJECTED'
    assert auth.save(OTHER) is None and auth.token() == OTHER
    assert auth.clear() is None and vault.value is None and auth.status().state == 'NONE'
    for obj in (auth, restarted, StaticAuth(TOKEN), auth.status()):
        assert TOKEN not in repr(obj) and TOKEN not in str(obj)

def test_session_failure_preserves_stored_and_clear_memory_first():
    vault = Vault(); vault.value = OTHER; vault.fail = True
    auth = GitHubAuth(vault); auth.load()
    assert auth.status().label == 'Test vault' and auth.status().problem
    assert auth.save(TOKEN) and auth.token() == TOKEN and vault.value == OTHER
    assert auth.status().state == 'SESSION'
    assert auth.clear() and auth.token() is None and vault.value == OTHER
    assert auth.status().state == 'NONE' and auth.status().problem
    auth = GitHubAuth(None)
    assert auth.save(TOKEN) and auth.status().state == 'SESSION'
    assert auth.save('bad') and auth.token() == TOKEN

def test_static_rate_and_rejection():
    auth = StaticAuth(TOKEN); rate = RateLimit(5000, 4999, 123, True, 100)
    auth.observe(rate,TOKEN); assert auth.rate == rate
    auth.reject(OTHER); assert auth.token() == TOKEN
    auth.reject(TOKEN); assert auth.token() is None

@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Credential Manager')
def test_real_windows_throwaway_vault():
    vault = WindowsCredentialVault('CoveArchive/test-' + uuid4().hex)
    try:
        vault.delete(); assert vault.load() is None
        try: vault.save(TOKEN)
        except VaultError:
            if vault._c.get_last_error() in (5, 1312):
                pytest.skip('Credential Manager unavailable in this Windows logon (error 5/1312)')
            raise
        assert vault.load() == TOKEN
        vault.save(OTHER); assert vault.load() == OTHER
        auth = GitHubAuth(vault); auth.load(); assert auth.token() == OTHER
        assert auth.clear() is None and vault.load() is None
    finally:
        vault.delete()

@pytest.mark.parametrize('load_works', [True, False])
def test_failed_replace_never_deletes_older_credential(load_works):
    class SaveFailVault(Vault):
        deletes = 0
        def load(self):
            if not load_works: raise VaultError('Vault read unavailable.')
            return super().load()
        def save(self, token): raise VaultError('Vault save unavailable.')
        def delete(self): self.deletes += 1; super().delete()
    vault=SaveFailVault(); vault.value=OTHER; auth=GitHubAuth(vault)
    assert bool(auth.load()) == (not load_works)
    problem=auth.save(TOKEN)
    assert problem.endswith(' The token is used for this session only; fix storage and paste it again to keep it.')
    assert vault.value==OTHER and vault.deletes==0
    assert auth.token()==TOKEN and auth.status().state=='SESSION'
    if load_works:
        restarted=GitHubAuth(vault); assert restarted.load() is None and restarted.token()==OTHER
    assert auth.clear() is None and vault.value is None and vault.deletes==1

@pytest.mark.parametrize('save_fails', [False, True])
def test_new_token_resets_remembered_rate(save_fails):
    vault=Vault(); auth=GitHubAuth(vault); auth.save(TOKEN)
    auth.observe(RateLimit(5000,12,1234,True,100),TOKEN); vault.fail=save_fails
    auth.save(OTHER)
    assert auth.rate is None and auth.status().rate is None
    assert auth.token()==OTHER

def test_clear_failure_explains_saved_credential_recovery():
    vault=Vault(); auth=GitHubAuth(vault); auth.save(TOKEN); vault.fail=True
    problem=auth.clear()
    assert problem.endswith(' Any saved token was not removed; try Clear Token again.')
    assert auth.status().problem==problem and auth.token() is None and vault.value==TOKEN
    assert TOKEN not in problem

def test_static_rejection_requires_matching_token():
    auth=StaticAuth(TOKEN); auth.reject(OTHER); assert auth.rejected is False
    auth.reject(TOKEN); assert auth.rejected is True and auth.token() is None
    empty=StaticAuth(None); empty.reject(None); assert empty.rejected is False

@pytest.mark.parametrize('action',['clear','replace','save'])
def test_observation_is_bound_to_effective_credential(action):
    auth=GitHubAuth(None)
    if action!='save': auth.save(TOKEN)
    used=auth.token(); rate=RateLimit(5000,1,1234,used is not None,100)
    if action=='clear': auth.clear()
    else: auth.save(OTHER)
    auth.observe(rate,used); assert auth.rate is None
    auth.save(OTHER); auth.observe(rate,OTHER); assert auth.rate==rate
    auth.reject(OTHER); assert auth.rate is None
    auth.observe(rate,OTHER); assert auth.rate is None
    auth.observe(rate,None); auth.reject(OTHER); assert auth.rate==rate

@pytest.mark.parametrize('action',['reject','clear','replace'])
def test_blocking_save_never_blocks_token_and_newer_state_wins(action):
    import threading
    entered=threading.Event(); release=threading.Event(); read=threading.Event()
    class Blocking(Vault):
        def save(self,token): entered.set(); assert release.wait(2); super().save(token)
    auth=GitHubAuth(Blocking()); writer=threading.Thread(target=lambda:auth.save(TOKEN)); writer.start()
    try:
        assert entered.wait(1)
        reader=threading.Thread(target=lambda:(auth.token(),read.set())); reader.start()
        assert read.wait(.3), 'token() blocked behind vault I/O'
        if action=='reject': auth.reject(TOKEN)
        elif action=='clear':
            clearer=threading.Thread(target=auth.clear); clearer.start()
        else: auth._vault=None; auth.save(OTHER)
    finally:
        release.set(); writer.join(2)
        if action=='clear': clearer.join(2)
    assert auth.status().state==dict(reject='REJECTED',clear='NONE',replace='SESSION')[action]

@pytest.mark.parametrize('operation',['load','clear'])
def test_blocking_load_delete_do_not_hold_auth_lock(operation):
    import threading
    entered=threading.Event(); release=threading.Event(); read=threading.Event()
    class Blocking(Vault):
        def load(self):
            value=self.value; entered.set(); assert release.wait(2); return value
        def delete(self): entered.set(); assert release.wait(2); raise VaultError('Storage unavailable.')
    auth=GitHubAuth(Blocking()); auth.save(TOKEN)
    worker=threading.Thread(target=getattr(auth,operation)); worker.start()
    try:
        assert entered.wait(1)
        reader=threading.Thread(target=lambda:(auth.token(),read.set())); reader.start()
        assert read.wait(.3)
        saver=threading.Thread(target=lambda:auth.save(OTHER)); saver.start()
    finally:
        release.set(); worker.join(2); saver.join(2)
    assert auth.token()==OTHER and auth.status().state=='SAVED' and auth.status().problem is None

@pytest.mark.parametrize('first',['save','clear'])
def test_vault_io_order_preserves_latest_persisted_state(first):
    import threading
    entered=threading.Event(); release=threading.Event(); newer_started=threading.Event()
    read=threading.Event(); errors=[]
    class Blocking(Vault):
        def save(self,token):
            if first=='save': entered.set(); assert release.wait(2)
            super().save(token)
        def delete(self):
            if first=='clear': entered.set(); assert release.wait(2)
            super().delete()
    vault=Blocking(); auth=GitHubAuth(vault)
    def run(operation,started=None):
        if started: started.set()
        try: operation()
        except Exception as exc: errors.append(exc)
    older=threading.Thread(target=run,args=(lambda:auth.save(TOKEN) if first=='save' else auth.clear(),))
    newer=threading.Thread(target=run,args=(lambda:auth.clear() if first=='save' else auth.save(OTHER),newer_started))
    older.start()
    try:
        assert entered.wait(1); newer.start(); assert newer_started.wait(1)
        # Read while the older operation is blocked and the newer one is waiting.
        reader=threading.Thread(target=lambda:(auth.token(),read.set())); reader.start()
        assert read.wait(.3), 'token() blocked behind vault I/O'
        reader.join(1)
    finally:
        release.set(); older.join(2)
        if newer.ident is not None: newer.join(2)
    assert not errors and not older.is_alive() and not newer.is_alive()
    expected=None if first=='save' else OTHER
    assert vault.value==expected
    assert auth.token()==expected and auth.status().state==('NONE' if first=='save' else 'SAVED')

def test_auth_has_no_shared_visibility_state():
    for auth in (GitHubAuth(None),StaticAuth(TOKEN)):
        assert all(not hasattr(auth,name) for name in ('_public','mark_public','forget_public','is_public'))
