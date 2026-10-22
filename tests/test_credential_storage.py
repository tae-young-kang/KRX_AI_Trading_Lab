"""Fake storage only: never access the user's Windows credentials."""
import pytest
from app import config, credential_check


def test_missing_storage_has_actionable_help_and_does_not_cache(monkeypatch):
    def missing(): raise ImportError('synthetic missing package')
    monkeypatch.setattr(config,'secure_storage',missing)
    monkeypatch.setattr(config.sys,'frozen',False,raising=False)
    store=config.Credentials()
    with pytest.raises(RuntimeError,match='run_windows.bat'):
        store.save('synthetic','fixture-value')
    assert store.session_secrets=={} and store.get('synthetic')==''
    store.save('synthetic','memory-only',persist=False)
    assert store.get('synthetic')=='memory-only'


def test_packaged_help_does_not_tell_user_to_install_python(monkeypatch):
    monkeypatch.setattr(config.sys,'frozen',True,raising=False)
    message=config.storage_install_help()
    assert '최신 실행 파일' in message and 'run_windows.bat' not in message


def test_failed_write_does_not_replace_previous_session_value_or_expose_error(monkeypatch):
    class Backend:
        def set_password(self,*args): raise ValueError('synthetic-private-diagnostic')
    monkeypatch.setattr(config,'secure_storage',Backend)
    store=config.Credentials()
    store.save('synthetic','previous',persist=False)
    with pytest.raises(RuntimeError) as error:
        store.save('synthetic','replacement')
    assert 'synthetic-private-diagnostic' not in str(error.value)
    assert error.value.__suppress_context__
    assert store.get('synthetic')=='previous'


def test_successful_write_uses_existing_service_and_then_caches(monkeypatch):
    writes=[]
    class Backend:
        def set_password(self,*args): writes.append(args)
    monkeypatch.setattr(config,'secure_storage',Backend)
    store=config.Credentials()
    store.save('synthetic','fixture-value')
    assert writes==[(config.APP_NAME,'synthetic','fixture-value')]
    assert store.get('synthetic')=='fixture-value'


@pytest.mark.parametrize('failure',[None,ImportError,RuntimeError])
def test_installer_check_constructs_only_and_returns_exit_status(monkeypatch,capsys,failure):
    calls=[]
    def construct():
        calls.append('construct')
        if failure: raise failure('synthetic-private-diagnostic')
        return object()  # Has no credential read/write methods.
    monkeypatch.setattr(credential_check,'secure_storage',construct)
    assert credential_check.main()==(1 if failure else 0)
    assert calls==['construct']
    assert 'synthetic-private-diagnostic' not in capsys.readouterr().out
