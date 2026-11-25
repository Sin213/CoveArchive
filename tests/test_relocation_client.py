"""Anonymous metadata transport and strict identity consistency."""
import pytest
import requests
from covearchive.github import client as g
from covearchive.github.urls import RepositoryTarget
from test_github_client import FakeResponse, FakeSession

TARGET = RepositoryTarget('Owner', 'Repo')


def metadata(**changes):
    return dict(id=1, name='Repo', full_name='Owner/Repo', html_url='https://github.com/Owner/Repo',
                owner=dict(login='Owner'), private=False, visibility='public') | changes


def client(*responses):
    return g.GitHubRepositoryClient(FakeSession(responses, timeout=(5.0, 10.0)))


def test_metadata_by_url_and_numeric_id_is_anonymous():
    c = client(FakeResponse(body=metadata()), FakeResponse(body=metadata()))
    assert c.auth is None and c.repository(TARGET) == (1, TARGET)
    assert c.repository(repository_id=1) == (1, TARGET)
    assert [call['url'] for call in c.session.calls] == [
        'https://api.github.com/repos/Owner/Repo', 'https://api.github.com/repositories/1']
    assert len(c.session.responses) == 2 and all(response.closed == 1 for response in c.session.responses)


@pytest.mark.parametrize('visibility', ['internal', None], ids=['internal', 'missing'])
def test_metadata_requires_explicit_public_visibility(visibility):
    body = metadata(visibility=visibility)
    if visibility is None:
        del body['visibility']
    with pytest.raises(g.InvalidResponseError):
        client(FakeResponse(body=body)).repository(TARGET)


@pytest.mark.parametrize('verify', [False, None, '', 1],
                         ids=['disabled', 'unset', 'empty-ca-path', 'non-path'])
def test_insecure_verification_settings_are_rejected_before_dispatch(verify):
    c = client(FakeResponse(body=metadata()))
    c.session.verify = verify
    with pytest.raises(g.InvalidTargetError):
        c.repository(TARGET)
    assert not c.session.calls


@pytest.mark.parametrize('setting, value', [
    ('params', {'access_token': 'fake'}),
    ('cert', ('client.crt', 'client.key')),
    ('auth', ('user', 'password')),
    ('headers', {'authorization': 'Bearer fake'}),
    ('headers', {'Cookie': 'session=fake'}),
    ('headers', {'Proxy-Authorization': 'Basic fake'}),
    ('cookies', {'session': 'fake'}),
], ids=['query', 'certificate', 'auth', 'authorization', 'cookie-header', 'proxy-authorization', 'cookies'])
def test_credentialed_session_settings_are_rejected_before_dispatch(monkeypatch, setting, value):
    with g.DirectSession() as session:
        if setting in ('headers', 'cookies'):
            getattr(session, setting).update(value)
        else:
            setattr(session, setting, value)
        calls = []

        def send(prepared, **kwargs):
            calls.append(prepared.url)
            response = FakeResponse(body=metadata())
            response.url = prepared.url
            return response

        monkeypatch.setattr(session, 'send', send)
        with pytest.raises(g.CredentialedSessionError):
            g.GitHubRepositoryClient(session).repository(TARGET)
        assert not calls


@pytest.mark.parametrize('effective_url', [
    'https://api.github.com/repos/Owner/Repo?access_token=fake',
    'https://api.github.com/repositories/1',
    'https://example.test/repos/Owner/Repo',
], ids=['query', 'different-path', 'different-host'])
def test_substituted_response_url_is_rejected_before_reading(effective_url):
    response = FakeResponse(body=metadata())
    response.url = effective_url
    with pytest.raises(g.UnsafeRedirectError):
        client(response).repository(TARGET)
    assert response.closed == 1 and response.reads == 0


def test_safe_redirect_is_routing_not_identity_proof():
    c = client(FakeResponse(301, headers={'Location': '/repositories/1'}),
               FakeResponse(body=metadata(name='New', full_name='Owner/New', html_url='https://github.com/Owner/New')))
    assert c.repository(TARGET) == (1, RepositoryTarget('Owner', 'New'))


@pytest.mark.parametrize('location', ['https://evil.test/repositories/1', 'http://api.github.com/repositories/1',
    'https://user@api.github.com/repositories/1', '//evil.test/repos/O/R', '/repositories/1?token=secret',
    '/repositories/1#fragment', '/repositories/1/releases', '/repos/O/%2f', '/repositories/0',
    '/repositories/9223372036854775808', '/repositories/1\\evil', '/repositories/1\n',
    '/repos/O/R.git', 'https://api.github.com:443/repositories/1'])
def test_unsafe_redirects_rejected_without_follow(location):
    c = client(FakeResponse(301, headers={'Location': location}))
    with pytest.raises(g.UnsafeRedirectError): c.repository(TARGET)
    assert len(c.session.calls) == 1 and c.session.responses[0].closed == 1


def test_empty_query_redirect_rejected_before_urljoin(monkeypatch):
    monkeypatch.setattr(g, 'urljoin', lambda *_: 'https://api.github.com/repositories/1')
    c = client(FakeResponse(301, headers={'Location': '/repositories/1?'}),
               FakeResponse(body=metadata()))
    with pytest.raises(g.UnsafeRedirectError): c.repository(TARGET)
    assert len(c.session.calls) == 1


@pytest.mark.parametrize('changes', [dict(id=True), dict(id=0), dict(id=-1), dict(id=2**63), dict(id='1'),
    dict(name='Other'), dict(full_name='Other/Repo'), dict(html_url='https://user@github.com/Owner/Repo'),
    dict(html_url='https://gitlab.com/Owner/Repo'), dict(html_url='https://github.com/Owner/Repo?query'),
    dict(owner=dict(login='Other')), dict(owner=dict(login='Owner/Injected')), dict(private=True),
    dict(private=0), dict(name=['Repo']), dict(owner=None)])
def test_inconsistent_or_malformed_metadata_rejected(changes):
    with pytest.raises(g.InvalidResponseError): client(FakeResponse(body=metadata(**changes))).repository(TARGET)


@pytest.mark.parametrize('raw', [b'{"id":1,"id":2}', b'{"id":NaN}', b'[' * 70 + b']' * 70,
    b'x' * (256 * 1024 + 1), b'[]', b'{}'],
    ids=['duplicate-key', 'nan', 'deep-nesting', 'oversized', 'array', 'empty-object'])
def test_metadata_json_bounds(raw):
    with pytest.raises(g.InvalidResponseError): client(FakeResponse(body=raw)).repository(TARGET)


def test_stable_id_conflict_and_redirect_id_conflict_rejected():
    with pytest.raises(g.InvalidResponseError): client(FakeResponse(body=metadata(id=2))).repository(repository_id=1)
    c = client(FakeResponse(301, headers={'Location': '/repositories/2'}), FakeResponse(body=metadata()))
    with pytest.raises(g.InvalidResponseError): c.repository(TARGET)


@pytest.mark.parametrize('status,error', [(404, g.NotFoundError), (403, g.AccessDeniedError), (429, g.RateLimitError),
    (500, g.TransientServerError)])
def test_typed_failures_no_retries(status, error):
    c = client(FakeResponse(status))
    with pytest.raises(error): c.repository(TARGET)
    assert len(c.session.calls) == 1


def test_unreadable_403_cannot_authorize_relocation(tmp_path):
    from covearchive import relocation as r
    from test_relocation import A, Provider, check

    path = tmp_path / 'relocation.json'; first = check(path, Provider()); before = path.read_bytes()
    denied = FakeResponse(403, read_error=requests.Timeout())
    moved = FakeResponse(body=metadata(name='New', full_name='Owner/New', html_url='https://github.com/Owner/New'))
    api = client(moved, denied)
    result = r.check_location(A, path, client_factory=lambda: api)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.verified_url is None
    assert result.locations == first.locations and path.read_bytes() == before
    assert denied.reads == denied.closed == 1


def test_network_error_no_retry_and_no_auth_argument():
    c = client(requests.ConnectionError())
    with pytest.raises(g.NetworkError): c.repository(TARGET)
    assert len(c.session.calls) == 1
    with pytest.raises(TypeError): g.GitHubRepositoryClient(auth=object())


def test_request_budget_and_redirect_loop_are_bounded():
    c = client(*(FakeResponse(body=metadata()) for _ in range(8)))
    for _ in range(8): c.repository(TARGET)
    with pytest.raises(g.IncompleteScanError): c.repository(TARGET)
    c = client(FakeResponse(301, headers={'Location': '/repos/Owner/Repo'}))
    with pytest.raises(g.UnsafeRedirectError): c.repository(TARGET)


@pytest.mark.parametrize('rid', [True, 0, -1, 2**63, '1'])
def test_invalid_known_ids_never_request(rid):
    c = client()
    with pytest.raises(g.InvalidTargetError): c.repository(repository_id=rid)
    assert not c.session.calls
