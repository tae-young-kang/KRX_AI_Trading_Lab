"""Offline checks with injected distributions/imports; no service clients."""
from pathlib import Path
from importlib import metadata
import pytest
from app import environment_check as module

VERSIONS={'PySide6':'6.6.0','requests':'2.31.0','keyring':'25.7.0',
          'websockets':'15.0.1','exchange_calendars':'4.13.2'}


def inspect(**kwargs):
    return module.check(package_version=VERSIONS.__getitem__,module_import=lambda name:object(),
        backend=lambda:object(),calendar=lambda day:tuple(str(i) for i in range(7)),platform='nt',
        python_version=(3,11,0),**kwargs)


def test_offline_runtime_check_imports_only_declared_components_without_credential_access():
    imports=[];backends=[]
    class Guard:
        def __getattribute__(self,name):raise AssertionError('must not access credential methods')
    result=module.check(package_version=VERSIONS.__getitem__,module_import=imports.append,
        backend=lambda:backends.append(True) or Guard(),calendar=lambda day:tuple(str(i) for i in range(7)),
        platform='nt',python_version=(3,11,0))
    assert result['ready'] and len(result['checks'])==9 and len(backends)==1
    assert set(imports)==set(module.MODULES.values()) and 'PySide6.QtWidgets' not in imports
    assert '키 읽기/쓰기' in result['checks'][-2]['detail']


@pytest.mark.parametrize('damage',['missing','old','pinned','broken_import','backend','calendar','prerelease'])
def test_unusable_installation_is_not_reported_ready_and_errors_are_redacted(damage):
    private_marker='PRIVATE_EXCEPTION_CONTENT_MUST_NOT_APPEAR'
    def version(name):
        if name=='websockets' and damage=='missing':raise metadata.PackageNotFoundError(private_marker)
        if name=='keyring' and damage=='old':return '23.0'
        if name=='exchange_calendars' and damage=='pinned':return '4.13.1'
        if name=='PySide6' and damage=='prerelease':return '6.6.0rc1'
        return VERSIONS[name]
    def load(name):
        if damage=='broken_import' and name=='PySide6.QtCore':raise OSError(private_marker)
    def backend():
        if damage=='backend':raise OSError(private_marker)
    def calendar(day):
        if damage=='calendar':raise ValueError(private_marker)
        return tuple(str(i) for i in range(7))
    result=module.check(package_version=version,module_import=load,backend=backend,calendar=calendar,
        platform='nt',python_version=(3,11,0))
    assert not result['ready'] and any(not row['ok'] for row in result['checks'])
    assert private_marker not in str(result)


def test_requirements_missing_or_unknown_is_not_a_partial_success(tmp_path):
    path=tmp_path/'requirements.txt'
    assert not inspect(requirements=path)['ready']
    path.write_text('requests>=2.31,<3\n',encoding='utf-8')
    assert not inspect(requirements=path)['ready']
    path.write_text('private_package>=1\n',encoding='utf-8')
    assert not inspect(requirements=path)['ready']


def test_cli_reports_readiness_and_failure_without_launching_anything(monkeypatch,capsys):
    monkeypatch.setattr(module,'check',lambda:dict(ready=False,checks=[dict(label='websockets',ok=False,detail='패키지가 없어요.')]))
    assert module.main()==1
    text=capsys.readouterr().out
    assert '-m pip install -r requirements.txt' in text and '시세 조회/텔레그램/주문을 실행하지 않아요' in text
    monkeypatch.setattr(module,'check',lambda:dict(ready=True,checks=[]))
    assert module.main()==0 and '설치 구성 확인 완료' in capsys.readouterr().out


def test_install_only_launcher_never_launches_gui_and_run_checks_exit_status():
    root=Path(module.__file__).resolve().parent.parent
    installer=(root/'install_windows.bat').read_text()
    runner=(root/'run_windows.bat').read_text()
    assert 'main.py' not in installer and '-m app.environment_check' in installer
    assert 'if errorlevel 1 exit /b 1' in installer
    assert 'call "%~dp0install_windows.bat"' in runner
    assert runner.index('if errorlevel 1')<runner.index('%PY% main.py')
