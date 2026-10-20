"""Installer check only; never read or write any stored credentials."""
from app.config import secure_storage, storage_install_help


def main():
    try:
        secure_storage()
    except ImportError:
        print(storage_install_help())
        return 1
    except Exception:
        print('Windows 보안 보관함을 준비하지 못했어요. 설치 환경을 확인해 주세요.')
        return 1
    print('보안 저장 구성요소 확인 완료 (인증정보 조회·저장 안 함)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
