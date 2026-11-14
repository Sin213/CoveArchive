from types import SimpleNamespace as NS
import pytest
from covearchive.queue import models as m

@pytest.mark.parametrize('status,code,retryable,kind', [
    ('ARCHIVED',None,False,'completed'),('ALREADY_ARCHIVED',None,False,'completed'),
    ('VERIFIED_EXISTING',None,False,'completed'),('CANCELLED','cancelled',False,'cancelled'),
    ('FAILED','network',True,'retry'),('FAILED','network',False,'failed'),
    ('FAILED','unknown_cdn_host',True,'failed'),('FAILED','disk_full',True,'failed'),
    *[(s,'hash_mismatch',True,'failed') for s in ('MODIFIED','CONFLICT','MISMATCH','UPSTREAM_REMOVED','RECONCILE_REQUIRED')]])
def test_Q20_classification(status,code,retryable,kind):
    outcome=m.classify_result(NS(status=status,code=code,retryable=retryable,retry_after_seconds=73))
    assert (outcome.kind,outcome.code,outcome.retry_after_seconds)==(kind,code,73)

@pytest.mark.parametrize('name,value', [('max_active',0),('max_active',9),('max_active',True),
    ('max_attempts',0),('max_attempts',21),('retry_base_seconds',-1),('retry_max_seconds',float('inf')),
    ('busy_timeout_seconds',float('nan')),('shutdown_timeout_seconds',-1),('retry_base_seconds',True)])
def test_Q18_settings_invalid(name,value):
    with pytest.raises(m.QueueError): m.QueueSettings(**{name:value})

def test_Q17_backoff():
    s=m.QueueSettings()
    assert [m.backoff_seconds(n,s,lambda:0) for n in (1,2,3,10)]==[5,10,20,300]
    assert m.backoff_seconds(2,s,lambda:0.5)==11
    assert m.backoff_seconds(7,s,lambda:1)==300

def test_Q29_bad_code():
    r=m.classify_result(NS(status='FAILED',code='https://secret?token=x',retryable=False,retry_after_seconds=None))
    assert r.code=='unknown_error'

def test_Q18_FB_limited_policy():
    assert getattr(m,'LIMITED_RETRY',{})=={'manifest_commit_incomplete':2}
