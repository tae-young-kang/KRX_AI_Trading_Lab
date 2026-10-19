# KRX AI Trading Lab — Work/Codex 연속 개발 진입점

갱신: 2026-10-19 (Asia/Seoul).

## 지금 바로 읽을 것

1. 이 저장소의 `AGENTS.md` — 보안·주문금지·테스트·커밋 규칙
2. `DEVELOPMENT_ROADMAP.md` — 사용자 승인 주 개발 단계 0~12, 우선순위
3. `DEVELOPMENT_HANDOFF.md` — 완료 이력과 미완료 검증, 재개 지점
4. 실제 Git의 `main`, 작업 브랜치, 열린 PR, 미커밋 변경 및 원격 HEAD

**이번 원격 이어하기:** Draft PR #1
https://github.com/tae-young-kang/KRX_AI_Trading_Lab/pull/1
브랜치: `codex/work-handoff-backup-inspection-20261019`
분기 기준 main SHA: `d3724fdf5fd86ef6af76181d3ba81bd368926563`.
작업 결과를 아직 main에 합치지 않았습니다.

## 가장 먼저 할 일

- Draft PR #1의 **실제 변경 파일**과 현재 브랜치를 확인하세요. main에 신규 커밋이 있으면 충돌을 먼저 점검하세요.
- `app/record_backup.py`, `tests/test_record_backup_inspection.py`, `app/gui.py`, `docs/RECORD_BACKUP.md`의 변경을 검토하세요.
- 전체 **오프라인** 테스트를 실행하세요. GUI 코드를 실행하지 않고 `python -m compileall -q app tests`로 문법을 검증하세요.
- Windows PC에서 실제 GUI의 **저장한 백업 확인하기** 버튼을 눌러 합성 백업으로만 동선을 확인하세요. 이 단계는 아직 진행되지 않았습니다.
- 검증 전 PR #1을 승인·병합하거나 실제 계좌·시장에 접근하지 마세요.
- 문제가 있으면 별도 커밋으로 수정하고 실제 테스트 숫자/실패를 기록하세요. 실제로 실행하지 않은 검사를 통과로 기재하지 마세요.

## 개발의 상위 목적

- 수익 가능성 높은 KOSPI/KOSDAQ 종목을 정확히 선별하고, 추천의 실제 이후 성과를 기록/검증하는 것이 최우선.
- 원래 단계 5의 키움 **모의** 체결/취소/HTS 대조 실증은 미완료이고 PC/사용자 승인·장중 환경이 필요합니다.
- 기다리는 동안 단계 7~8의 추천 이력·확인된 시세·거래일·기업행동/비용 자료 검증을 병행합니다.
- 테마1은 선택 사항입니다. 기본 스크리닝을 대체하거나 테마1 전략을 자동 활성화하면 안 됩니다.
- 모드별 데이터, 쉬운 한국어 GUI/텔레그램, 비밀키 보안과 모든 실계좌 주문 잠금을 유지합니다.

## 작업 방식

- 매 소목표에 대해 현재 상태→작업 분기→작은 수정→오프라인 검증→diff/정확한 변경 내용 비밀정보 검사→필요한 파일만 커밋→원격 SHA 확인→인수인계 갱신.
- 병렬 작업과 기존 사용자 수정을 덮어쓰지 않습니다. 원격 강제 푸시·재작성·계정 연결·실거래 금지.
- `DEVELOPMENT_HANDOFF.md`의 이력은 보존하고 뒤에 새 절을 추가하세요. 마지막에 커밋·테스트·실증 상태·다음 작업을 명시하세요.
- 이 문서는 원격 장시간 자동 실행 허가가 아닙니다. 한 대화/Work 실행에서 허용된 범위와 도구 한계 안에서 순차 개발하고, 중단 시 정확한 상태를 저장하세요.

## 이번 원격 작업의 검증 수준

격리된 최소 의존성 복사본에 대해 **총 6개 오프라인 테스트 통과**:
- 백업 검증 3개 (모드, 건수, 변조, 잘못된 파일, 심볼릭 링크)
- 추천 관측 날짜별 균등 평균 3개 (종목/날짜 가중 차이, 미관측 상태 제외)
- `python -m compileall -q app tests`로 이 격리 검사 복사본 문법 검사 통과.

이 수치는 원격 GitHub 저장소의 전체 테스트 통과를 뜻하지 않습니다.
실제 전체 저장소에 대해 테스트를 실행할 수 있는 Work/PC 환경에서 다시 확인해야 합니다.
**전체 프로젝트 테스트, 실제 Windows PySide6 GUI, 개인 기록 DB, 텔레그램·시장 통신 및 실제 모의 체결은 이 세션에서 실행하지 않았습니다.**
`main`에 남아 있는 과거 '747개 통과'는 과거 커밋 기준 기록이지 이 PR 변경에 대한 전체 검증이 아닙니다.

## 집에 가서 Work에 붙여넣을 지시

이 GitHub 저장소와 Draft PR #1을 확인하세요. 먼저 AGENTS.md, DEVELOPMENT_ROADMAP.md,
DEVELOPMENT_HANDOFF.md, WORK_CONTINUATION.md를 읽고 Git 상태를 대조하세요.
PR #1의 백업 검증 변경을 전체 오프라인 테스트 및 합성 GUI 환경에서 점검하고,
실계좌 주문·실 API·비밀키 업로드 없이 미완료 검증을 마무리하세요.
이후 원래 0~12단계 로드맵에서 단계5 모의 검증 준비와 단계7~8 추천 성과/자료 검증을
우선하여 작은 단위로 구현/검사/커밋하고, 진행 상황을 인수인계 문서에 누적하세요.
사용자 기존 변경과 작업 브랜치가 충돌하면 확인 전 덮지 말고, 완료하지 않은
실제 모의 검증·GUI 실증을 완료했다고 쓰지 마세요.

## 이번 세션에서 추가 개발된 단계 7 기능

- `app/recommendation_overview.py`: 1/3/5거래일 관측을 추천일별로 재집계해 `daily_mean`과 `observed_days`를 반환합니다. 기존 `mean`(종목 동일가중)은 그대로 유지합니다.
- `/추천요약`: 날짜별 균등 평균을 기존 종목별 평균과 병렬 표기하고 실제 평가 추천일 10일 미만이면 경고합니다.
- `tests/test_recommendation_day_average.py`: 가상의 3종목 +10% / 다음날 1종목 -10%에서 종목 평균 +5%, 날짜 평균 0% 검증 및 미완료/누락 자료의 0% 오인 방지.
- `docs/RECOMMENDATION_TRACKING.md`: 정의/한계 문서화.
- 이 기능은 추천 정확도 평가용 기술통계일 뿐 AI 학습, 주문 연결 또는 실제 수익률 보증이 아닙니다.
- Work에서 **전체 기존 테스트를 다시 실행**하고 텔레그램 요약 및 GUI와의 하위 호환성을 확인하세요. 기존 `test_recommendation_overview.py`를 포함하세요.

집 PC에 아직 작업 브랜치가 체크아웃되어 있지 않다면 main 상태를 먼저 확인하고 PR #1 브랜치를 명시적으로 선택해 검사하세요. `main`의 기존 사용자가 변경한 파일을 덮어쓰지 마세요.

## 2026-10-19 최신 추가 개발: 부분 관측 편향 방지 + CI 준비

- `app/recommendation_overview.py`: 기존 `mean` 및 `daily_mean`은 유지하고 `complete_days`, `complete_daily_mean`을 추가했습니다. 이때 같은 조건·같은 날짜로 선택된 모든 추천(가격/거래일 출처가 다르거나 아직 평가되지 않은 종목도 포함)을 분모로 사용합니다.
- 날짜별 평균은 일부 종목만 평가돼도 계산하는 기술통계이므로, 관측 완료된 추천일만 별도로 보여줘 편향 가능성을 명시합니다. 조회되는 최근 3,000개 기록 안에서만 완전성 판정합니다.
- `tests/test_recommendation_day_average.py`: 날짜별 부분 관측/대기/출처 분리 경계검사 3개 추가.
- **이번 세션의 격리 오프라인 재검사: 9개 pytest 통과 + compileall 통과.** 전체 원본 저장소의 모든 테스트는 이 환경에서 직접 실행하지 못했으며, 실전 연결·GUI·실제 모의 주문도 하지 않았습니다.
- `.github/workflows/offline-checks.yml`: PR 변경 시 Python 3.11 환경에서 문법 검사와 `tests/test_scan_ui.py`를 제외한 오프라인 테스트를 실행하도록 CI 구성했습니다. 의존성 설치 뒤 테스트 단계의 일반적인 HTTP 프록시를 차단합니다. **GitHub Actions 실제 실행 결과는 확인 후 기록해야 합니다.**
- Work는 PR #1 GitHub 체크 결과를 먼저 확인하고 오류가 있으면 변경 범위를 한정해 수정하세요. Actions 성공도 사용자 PC Windows GUI/실제 모의 체결 검증을 대신하지 않습니다.
- 현재 변경사항은 여전히 Draft PR #1의 별도 브랜치에만 있고 실계좌 주문 잠금 및 민감정보 금지는 유지됩니다.

## 2026-10-19 최신 검증 완료 — 평가일 분모 보완 및 전체 CI

- 추천 정확도 품질 표시: 집계 내 `complete_days / selected_days`는 가격 관측이 **0건인 추천일도 분모에 포함**합니다. 조건이 같은 최초 추천들의 모든 선택 날짜를 기준으로 계산합니다. `observed_days` 및 기존 평균은 별도 보존합니다.
- 새 회귀 검사 `test_total_day_denominator_includes_dates_without_any_observations`를 추가했습니다.
- 개인 키/시장/증권사/텔레그램 연결 없이 **로컬 격리 Pytest 10개 통과 + compileall 통과**.
- GitHub Actions 전체 오프라인 테스트: 최초 **755 통과, 1 실패** (기존 텔레그램 경고 문구 호환성). 안내 문구 복구 후 `757 passed` 및 CI **success** 확인.
- **성공한 검증된 코드 SHA:** `3bf1b166bf2489242dada48fad9f03da79ef4d80`. 문서만 수정한 SHA `de23c00da8a67be965cf2aa041ed82efc2fddd58`의 CI도 success. 검증 링크: https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39223146708
- CI는 `tests/test_scan_ui.py`를 제외한 Python 3.11 오프라인 검증이며, **Windows 실 GUI 동선·실 키움 모의서버/체결·HTS 대조·복구 실증·실계좌는 여전히 미검증**.
- 최종 PR 검토 시 현재 원격 SHA, 열린 PR #1, AGENTS.md, DEVELOPMENT_ROADMAP.md, DEVELOPMENT_HANDOFF.md와 이 파일을 함께 읽고, 최신 전체 CI 상태를 다시 확인하세요.

## 2026-10-19 추가 개발: 추천 성과의 데이터 확보 상태 3단계

- 현재 PR #1에서 `app/recommendation_overview.py`의 1/3/5거래일 요약에 **완전 관측일 / 부분 관측일 / 이 출처 기준 미관측일**을 모두 표시하도록 개선했습니다.
- 정의: `complete_days + partial_days + unobserved_days == selected_days`. 일부라도 관측했지만 해당 조건·일자의 모든 선택 종목이 관측되지 않으면 부분 관측. 어떤 종목도 관측되지 않으면 미관측. **종목이 하나도 평가되지 않은 경우에도 0/N일을 보여줍니다.**
- 이 상태는 선택된 최근 3,000개의 최초 추천·조건 범위 안에서의 자료 보유 상태이며, 실제 전체시장 완전성/실제 계좌 수익을 증명하지 않습니다. 자료 출처별 그룹을 섞지 않습니다.
- `tests/test_recommendation_day_average.py`에 부분/미관측/0건·요약 표시 확인 검사 2개 추가. 기존 텔레그램 안내 문구 유지.
- 실제 GitHub Actions Python 3.11 전체 오프라인 테스트 `759 passed in 15.52s`, **success**: https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39225714845
- 성공 코드 SHA: `86a108a3525ffe3e3657ffabef0753f62f932a90`. 집 PC Work는 현재 브랜치의 실제 SHA, PR #1 diff, CI 로그를 다시 대조하세요.
- 검사 범위: `tests/test_scan_ui.py` 제외. Windows GUI·실 텔레그램·실제 키움 모의 체결/장부 대조·모든 실계좌 주문·개인 DB 검증은 여전히 미완료.
- 다음 단계는 조건별 관측 대상과 자료 출처/거래일 대조를 실데이터로 검증하되, 검증되지 않은 자료를 0% 수익률로 대체하거나 자동 매매 조건으로 적용하지 마세요.

## 2026-10-19 후속 개발: 거래량/이동평균의 거래일 누락 보호

- **원본 목표 관련 Stage 8 데이터 품질 수정**: `app/research.py`의 거래량비/20일 이동평균 특징값은 종목별 단순 최근 20봉이 아니라 **같은 시장에서 실제 관측된 바로 이전 20개 시장 거래일**과 종목 이력이 날짜별로 정확히 일치할 때만 계산합니다.
- 거래 중단/특정 종목 일봉 누락으로 오래된 과거 봉이 끼어 있으면 특징값/추천 후보 생성에서 제외하며, 20거래일 이력이 다시 연속 확보된 시점부터 자동 재포함합니다. 임의의 값으로 보간하지 않습니다.
- `quality['invalid_history']`는 이제 이전 거래량 0뿐 아니라 20시장 거래일 불연속으로 건너뛴 특징값도 포함합니다.
- `tests/test_research_market_session_history.py`: 일부 종목의 단일 시장일 누락 → 이후 20거래일 제외 → 복귀, KOSPI/KOSDAQ의 독립 관측 달력 검사 2개 추가.
- GitHub Actions 최신 관련 성공: **761 passed in 16.29s** (Python 3.11 오프라인 / `tests/test_scan_ui.py` 제외) https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39226344344
- 원본 시장 달력은 로드된 각 시장 전체 종목의 날짜 합집합에서 유추합니다. 모든 종목에서 같은 날 자료가 빠진 경우 감지할 수 없으므로 **공식 거래일 달력·KRX 전체 상장 목록·기업행동/수정주가 검증은 여전히 필요**합니다.
- PR #1 브랜치만 업데이트. 실계좌 주문·실 API 사용·비밀키 공개·Windows GUI 실행 없음.
- Work 재개 시 CI 재확인, 공식 거래일/전체 시장 범위 대조 및 독립 실제 데이터 검증을 Stage 8 후속 우선 작업으로 유지하세요.

## 2026-10-19 추가 개발: 외부 시장 거래일·최소 종목 수 읽기 대조 (Stage 8)

- 연구 화면의 **저장 일봉과 외부 거래일·최소 종목 수 대조 (고급)** 버튼을 구현했습니다. 사용자가 별도 확인한 CSV의 `market,day,min_symbols`를 현재 연구 자료 출처(`KRX`/`CSV_USER`/`DEMO_SYNTHETIC`)와 읽기 전용 비교합니다.
- `app/market_coverage.py`: 파일 형식/크기(1MB)/행수(10,000)/정확한 ISO 날짜/미래 날짜/중복 시장·날짜/종목 수 하한을 검사. SQLite 동일 읽기 트랜잭션에서 시장·날짜별 종목 개수 및 정규화 제외 수 기록을 읽어, 전체 누락/기준 미달/직전 입력 거래일 대비 30% 초과 감소/정규화 제외를 표시합니다.
- `app/gui.py`에 안내 버튼 연결. 불러오는 CSV는 읽기만 하고, DB를 갱신하거나 네트워크·계좌 주문을 실행하지 않습니다.
- `tests/test_market_coverage.py`: 전일 대비 급감/거래일 전체 누락/출처 간 분리/정규화 제외/외부 파일 오류·중복·미래 날짜/과도한 크기/표시 상한 등 14개 신규 pytest 검사 추가.
- `docs/HISTORY_COVERAGE_AUDIT.md`: CSV 형식/가상 예시/판정 정의/한계/사용 방법.
- **GitHub Actions 전체 오프라인 테스트 775 passed in 15.21s**, Python 3.11, `tests/test_scan_ui.py` 제외, https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39232225038 (검증 코드 SHA `bb406702e092537ea511f7b10eddf32fe57c11d2`).
- 중요: 사용자 CSV가 진짜 공식 자료인지 인증하지 않으며, 입력이 부정확하면 진단도 잘못됩니다. 하한 충족은 실제 모든 상장 종목/거래일/종가 확인의 증거가 아닙니다. 실제 개인 DB 및 Windows GUI는 실행해 보지 않았습니다.
- Draft PR #1 및 기존 안전 규칙 유지. 집 PC Work는 CI 상태와 PR diff/미커밋 자료를 확인하고, 실제 사용자 근거 파일이 준비되면 자료 자체의 출처/시장 범위를 별도 대조해 주세요.

## 2026-10-19 연속 개발: 48개 연구 조건의 실행 전 자료 품질 표시 (Stage 8)

- `app/research_preflight.py`: `check_research_coverage`로 연구 실행 **전에** 저장 일봉과 세션 중 선택된 외부 시장 거래일·최소 종목 수 CSV를 다시 대조합니다. 결과는 `NOT_CHECKED`(외부 근거 없음), `ALERT`(누락/기준 미달/미대조 날짜), `REFERENCE_FLOOR_MET`(제공된 하한 기준만 충족, 공식 완전성 아님).
- `app/gui.py`: 외부 대조 버튼이 성공적으로 끝나면 **동일 연구 자료 출처에만** 참조 CSV 경로를 세션 중 기억합니다. 다른 출처 선택 시 해제. 수동 48개 조건 연구 실행 전 품질 검사 후 결과의 `coverage_quality`에 저장해 메인 연구 요약과 완료 안내에 표시합니다. 경고가 있어도 탐색 연구는 가능하나 검증 완료로 취급하지 않습니다.
- GUI 자동 KRX 연구: 선택한 독립 KRX 참조 자료 검사에서 `ALERT` 또는 예기치 않은 품질 상태면 연구를 **보류**하고 저장하지 않습니다. 외부 참조 없는 자동 실행은 허용하되 결과에 `NOT_CHECKED`를 기록합니다.
- `tests/test_research_preflight.py`: 참조 없음/누락일/출처 분리/파일 손상/정규화 제외/세션 중 파일 삭제/자동 보류·구버전 기록 표시/DB 리포트 저장을 포함한 신규 오프라인 검사 9개.
- `docs/RESEARCH_PREFLIGHT.md`: UI 사용 순서/품질 상태/수동 vs 자동 정책/한계.
- GitHub Actions Python 3.11 오프라인 회귀 **784 passed in 15.99s** (환경에 의존하는 `tests/test_scan_ui.py` 제외). https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39237697971 (검증 코드 SHA `5f118ed52d7df7766453f9e82b13a1669f37ce77`).
- 외부 대조 CSV는 사용자 제공이므로 공식 KRX 거래일/전체 상장 종목의 출처 인증·기업행동/수정주가/생존편향·실제 수익의 독립 실증이 아닙니다. Windows GUI 실제 클릭, 개인 이력, 키움 실모의 체결/HTS 대조, 실계좌 주문은 **실행하지 않았습니다**.
- Work 인수인계: 최신 Draft PR #1의 브랜치 SHA·CI를 재확인하고 Windows GUI의 외부 대조→수동 연구 품질 경고/상태 저장→자동 보류 경로를 합성 자료로 확인해 주세요. 기존 main은 변경하지 않았고 강제 푸시/실주문/비밀키 업로드 금지 유지.

## 2026-10-19 추가 개발: 48개 조건 학습→검증 차이와 자료 품질 분석

- `app/research_comparison.py`: TRAIN/TEST의 3일 평균 초과수익 차이(`holdout_gap3=test_excess3-train_excess3`, %p)와 자료 전체 `coverage_quality`, 학습·검증 평가일 수(경고용 12/5일)로 각 조건의 **해석/주의 문구**를 계산합니다.
- **절대 재정렬/자동 승격하지 않음**: 원래 `evaluate_research`가 TRAIN 결과로만 결정한 후보·순서 및 `chosen_on_train_only`를 보존하고, TEST는 기술통계 분석용으로만 사용합니다. 48개 조건이 같은 시기/시장 자료에 기반해 독립 검증이 아님을 안내합니다.
- `app/gui.py` 연구 화면: `학습→검증 차이(%p)`, `자료/결과 해석` 2개 컬럼 추가. 선택한 조건 세부 설명과 상단 연구 요약에도 자료 확인/표본/다중조건 해석 한계 반영.
- `app/research.py` `export_report`: `summary.csv`에 `holdout_gap3`, `data_quality_label`, `comparison_note`, `adequate_train_days`, `adequate_test_days` 추가. 이전 컬럼·행 순서 유지.
- `tests/test_research_comparison.py`: 가상의 TRAIN 양수→TEST 음수, 검증이 좋은 다른 후보에 의해 **원래 TRAIN 순서가 바뀌지 않음**, 자료 이상 우선 안내, 과거 품질 필드 없는 리포트, 표본 부족, 비정상/극단적 수치, CSV 내보내기 등 검사 추가.
- `docs/RESEARCH_COMPARISON.md`: 지표 정의, 품질 상태, 다중조건 과적합·통계적 비독립성·누적수익이 아님을 설명.
- GitHub Actions **806 passed in 29.46s**, 성공 검증 코드 SHA `378d0b8e7e0981abdb5cc6f441aa792a5da7d97d`, https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39239155394 . 이후 문서만 추가되었음.
- 환경: Python 3.11 오프라인 pytest (`tests/test_scan_ui.py` 제외). 실 Windows GUI, 사용자 원본 시세의 신뢰도, 키움 실제 모의 체결/HTS 대조, 실계좌 주문, 개인 인증정보는 **사용·검증하지 않았음**.
- 다음 Work 목표: 실제 Windows 합성 데이터 GUI에서 연구 표·선택 변경/품질 없는 과거 리포트/CSV 내보내기를 검증하고, 단계 7~8의 **정확한 실제 데이터로 독립 전략 성능 검증**과 단계 5의 모의 체결 준비를 순차 진행. PR #1 리뷰 전 `main`에 병합하지 마세요.

## 2026-10-19 다음 연속 개발: TEST 초반·후반 기간 안정성 진단 (Stage 7~9)

- `app/research_stability.py`: TEST 시장 거래일의 시간 순서상 **앞/뒤 절반**을 동일시장 24개 조건에 같은 기준으로 적용합니다. 종목 추천이 발생한 날짜만 골라 임의로 절반씩 자르지 않습니다.
- 각 조건의 일별 3거래일 **시장대비 초과수익**을 초반/후반 관측 날짜별 동일가중으로 평균합니다. 이 값은 `top_signals.r3`의 순수익이 아니므로 섞지 않습니다. 미관측 날짜는 0%로 채우지 않습니다.
- `app/research.py`: `test_early_excess3`, `test_late_excess3`, 각 실제 관측일 수, `test_late_gap3`, `test_stability_note`를 48개 조건 결과에 추가. TRAIN 기반 원래 `variants` 정렬과 `chosen_on_train_only`는 **절대 변경하지 않음**. `summary.csv`에도 새 필드 내보내기.
- `app/gui.py`: `TEST 초반→후반` 열 및 선택 조건의 세부 초과수익/관측일/후반 안정성 주의 문구 표시. 구버전 보고서에는 구간 자료가 없음으로 표시.
- `app/research_comparison.py`: 기존 TEST 구간과 새 초반·후반은 독립 검증이 아니라는 문구 보강.
- `tests/test_research_stability.py`: 시장 세션 기준 분할, 미관측/표본 부족/중복 날짜/범위 밖/overflow, 실제 연구 엔진 TRAIN 기준 후보 보존, CSV 순서 포함 **8개 새 검사**.
- 최초 실행 시 합성 테스트가 KOSPI 한 시장만 입력하면서 48조건을 기대하는 검사 오류가 발생했으나, **단일 시장 24조건이 올바름을 확인해 수정**했습니다. 최종 GitHub Actions Python 3.11 전체 오프라인 검사 **814 passed in 17.65s**, 코드 SHA `8dc0cae6c48127a4cf4e4f0375c3acfb42af8243`, https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39241322674
- 상세 제약: `docs/RESEARCH_TEST_STABILITY.md`. TEST를 다시 쪼개 본 기술통계이지 진정한 신규 기간 walk-forward 검증이 아닙니다. 시장 전체 누락 거래일·생존편향/기업행동·데이터 허가/거래 가능성은 확인되지 않았습니다.
- 이 세션에서 사용자 Windows GUI 클릭·실제 KRX 자료/Kiwoom 모의 주문·HTS 체결 대조·실계좌 주문/비밀키 사용 **없음**. PR #1 작업 브랜치만 변경, main 보존.
- 집 Work는 PR #1 HEAD/CI/diff/미커밋 변경을 먼저 대조하고 Windows 오프라인 합성 GUI/CSV를 검증한 뒤, 독립 실제 KRX 자료 기반 시간 경과 검증과 키움 모의 체결 실증 준비를 순차 수행하세요.

## 2026-10-19 집 PC 재개: PR #1 Windows 오프라인 검증 보강

- Draft PR #1/원격 HEAD `4e8ceba`와 main `d3724fd`를 확인하고 PR 브랜치로 전환했습니다. main과의 변경 25개 파일을 확인했으며 미커밋 사용자 변경은 없었습니다.
- 기존 PR 최신 CI success 확인: https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39241674507
- 재개 기준 전체 로컬 Windows Python 3.15: 813 passed, 1 skipped. 건너뜀은 Windows 심볼릭 링크 생성 권한 제한이며 GUI 검사는 별도 제외입니다.
- 새 검증: 실제 연구 엔진 TEST 가격만 변화시켜 TRAIN 수치/순위/후보 불변, 실제 GUI 메서드를 가짜 위젯으로 실행해 표11열·상세·CSV 순서/수치 일치, 구버전 보고서 신규 필드 미조작 검사.
- 새 전체 검사: 816 passed, 1 skipped (21.17s), compileall 통과. QApplication/GUI/실제 키/시장/텔레그램/주문 실행 없음.
- GitHub Actions를 Linux+Windows Python3.11 행렬로 확장. 이번 코드의 원격 CI는 푸시 후 별도 확인할 예정입니다.
- 현재 단계5 실제 모의 체결/HTS 대조 미완료. 단계7~9의 기존 TRAIN-only 후보 및 TEST 진단을 재검증했으며 새 전략 채택/실주문은 하지 않았습니다.
- 다음: 새 원격 CI 확인, 독립 실제 자료/기업행동·시장 범위 검증과 모의 실제 실행 준비. 실제 GUI 클릭은 AGENTS.md의 버전관리 작업 중 GUI 실행 금지 때문에 완료로 표시하지 않습니다.

### 이번 푸시 확인 / 남은 CI 확인 지점

- 코드 커밋 `c60bab87b0444bb008c647ac3ccba108b017df4a`를 PR 브랜치에 푸시했고 로컬/원격 SHA 일치를 확인했습니다.
- 새 CI 실행: https://github.com/tae-young-kang/KRX_AI_Trading_Lab/actions/runs/39246278433
- Linux job `118038869742`는 success. Windows job `118038870263`은 설치/문법 검사 뒤 pytest 실행 중으로 확인했습니다. 전체 CI 성공으로 기록하지 않습니다.
- 재개 시 같은 run ID의 최종 상태/로그부터 확인하세요. 실행 중이라는 이유만으로 실패로 간주하거나 새 실행을 시작하지 마세요.
- PR 설명 갱신, Draft/미병합 유지. `main`과 개인 기록/연결 정보는 변경하지 않았습니다.

## 2026-10-19 Windows/Linux CI 확정 및 연구 추천 가격 확보 집계

- 직전 코드 c60bab8의 CI run39246278433 최종 success 확인: Linux817 passed(16.97s), Windows817 passed(175.26s). 로컬816+skip1과 구분합니다.
- 단계7~8 보완: 연구 추천 signals의 3거래일 결과를 관측/누락/학습경계 제외/자료끝쪽으로 집계하고 완전/일부/전체누락 추천일을 별도 표시.
- 기존 TRAIN-only 순위/후보와 평균 계산은 유지. 집계는 관측 분모 설명용이며 새로운 전략 성과나 자동 적용이 아닙니다.
- 선택 조건 상세/summary.csv에 집계 추가, 구버전은 기록 없음 안내. 실제 엔진에서 선택 종목의 미래봉을 삭제해 누락 포착 검사.
- 전체 로컬 Windows Python3.15:820 passed,1 skipped(21.08s), compileall 통과. 실제 GUI/키/시장/텔레그램/계좌 주문 미실행.
- 다음: 이번 변경의 Linux/Windows CI 확인, 공식 거래일/독립 원본·기업행동 검증 및 단계5 모의 실행 준비. Draft PR #1 유지, main 병합 없음.

## 2026-10-19 사용자 우선순위 수정: Windows와 전체 사용 흐름

- 사용자는 Linux 지원/검증이 필요 없다고 명시했습니다. CI를 windows-latest 단일 실행으로 변경합니다.
- 전체 완성 목표와 기존 로드맵은 유지하되, 모의투자 확장 및 연구 보조 진단의 반복 보강에 개발을 편중하지 않습니다.
- 다음 구현은 텔레그램 추천·조건 확인/변경, 테마1 검색·알림, 쉬운 사용 흐름의 기존 구현을 점검하고 실제 누락된 사용자 기능부터 완성합니다. 외부 종목코드 대조 확장은 이번 우선순위에서 보류합니다.
- 모의 기능 신규 확장은 보류하고 실제 주문 연결에 필요한 검증은 주문 연결 직전 별도 단계로 유지합니다. 안전 잠금 제거 또는 실계좌 주문 허가는 아닙니다.
- 이번 변경 검증: Windows 로컬 전체 오프라인 820 passed, 1 skipped (21.70s). GUI 검사는 별도 제외, skip은 심볼릭 링크 권한 제한. 실데이터/GUI/텔레그램/주문 실행 없음.
- 직전 eff116f CI run39247159823은 확인 시 Windows 실행 중이었으며, 새 Windows 전용 CI는 푸시 후 확인해야 합니다. 성공으로 간주하지 않습니다.
- 완료 절차: 정확한 작업 파일만 stage, diff/비밀정보 검사 후 커밋·푸시하고 원격 SHA 확인. PR #1 Draft 유지, main 병합 없음.
