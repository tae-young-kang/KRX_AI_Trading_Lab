"""Offline Windows installation checks. No settings, credentials or clients."""
from datetime import datetime
from importlib import import_module,metadata
import os
from pathlib import Path
import re
import sys

MODULES={'PySide6':'PySide6.QtCore','requests':'requests','keyring':'keyring',
         'websockets':'websockets','exchange_calendars':'exchange_calendars'}


def release(value):
    # Unknown/prerelease formats are unverified, never silently accepted.
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,3}',value):
        raise ValueError('패키지 버전 형식을 확인하지 못했어요.')
    parts=tuple(map(int,value.split('.')))
    return parts+(0,)*(4-len(parts))


def compatible(installed,spec):
    version=release(installed)
    for constraint in spec.split(','):
        match=re.fullmatch(r'(>=|<|==)([0-9]+(?:\.[0-9]+){0,3})',constraint)
        if not match:raise ValueError('지원하지 않는 패키지 조건이에요.')
        boundary=release(match[2])
        if not {'>=':version>=boundary,'<':version<boundary,'==':version==boundary}[match[1]]:return False
    return True


def check(*,requirements=None,package_version=None,module_import=None,backend=None,calendar=None,
          platform=None,python_version=None):
    """Dependencies injectable for tests; never query stored credential values."""
    rows=[]
    def row(label,ok,detail):rows.append(dict(label=label,ok=bool(ok),detail=detail))
    platform=os.name if platform is None else platform
    version=sys.version_info[:3] if python_version is None else python_version
    row('운영체제',platform=='nt','Windows용 설치 점검입니다.')
    row('Python',version>=(3,11),f'{version[0]}.{version[1]}.{version[2]} · 3.11 이상 필요')
    version_of=package_version or metadata.version
    load_module=module_import or import_module
    path=requirements or Path(__file__).resolve().parent.parent/'requirements.txt'
    try:
        specs=[]
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            match=re.fullmatch(r'([A-Za-z0-9_-]+)((?:>=|<|==).+)',line)
            if not match or match[1] not in MODULES:raise ValueError()
            specs.append((match[1],match[2]))
        if {name for name,_ in specs}!=set(MODULES) or len(specs)!=len(MODULES):raise ValueError()
    except (OSError,ValueError):
        row('설치 목록',False,'프로젝트의 requirements.txt를 확인해 주세요.')
        return dict(ready=False,checks=rows)
    for name,spec in specs:
        try:
            installed=version_of(name)
            if not compatible(installed,spec):
                row(name,False,'설치 버전이 프로젝트 설치 조건과 달라요.');continue
            load_module(MODULES[name])
            row(name,True,f'{installed} · 설치 조건 {spec} · 모듈 로딩 확인')
        except metadata.PackageNotFoundError:
            row(name,False,'패키지가 없어요. 설치 명령을 실행해 주세요.')
        except Exception:
            row(name,False,'패키지 버전 또는 모듈 로딩을 확인하지 못했어요. 다시 설치해 주세요.')
    if platform=='nt':
        try:
            if backend is None:
                from app.config import secure_storage
                backend=secure_storage
            backend()  # Construct only; never get/set/delete/enumerate secrets.
            row('보안 보관함 구성',True,'구성요소 생성 확인 · 키 읽기/쓰기와 실제 저장 성공은 검사하지 않음')
        except Exception:
            row('보안 보관함 구성',False,'Windows 보안 저장 구성요소를 준비하지 못했어요.')
    try:
        if calendar is None:
            from app.theme_calendar import recent_sessions
            calendar=recent_sessions
        from app.trading_clock import KST
        days=calendar(datetime.now(KST).date())
        if not isinstance(days,(tuple,list)) or len(days)!=7:raise ValueError()
        row('거래일 달력',True,'최근 7거래일 준비 확인 · 거래소 실시간 운영 확인은 아님')
    except Exception:
        row('거래일 달력',False,'현재 날짜의 거래일 달력을 준비하지 못했어요.')
    return dict(ready=all(item['ok'] for item in rows),checks=rows)


def main():
    result=check()
    print('[프로그램 설치 준비 점검]')
    for item in result['checks']:
        print(f'{"확인" if item["ok"] else "확인 필요"} · {item["label"]}: {item["detail"]}')
    print('설치 구성 확인 완료' if result['ready'] else '아직 준비되지 않은 항목이 있어요.')
    print('이 점검은 GUI/시세 조회/텔레그램/주문을 실행하지 않아요. 연결 성공 검사는 별도예요.')
    if not result['ready']:
        print('프로젝트 폴더에서 같은 Python으로 설치하세요:')
        print(f'& "{sys.executable}" -m pip install -r requirements.txt')
    return 0 if result['ready'] else 1


if __name__=='__main__':raise SystemExit(main())
