# text2sql-plugin

Claude Code와 Codex에서 자연어로 데이터베이스를 조회하는 플러그인입니다. 에이전트가 질문에 맞는 SQL을 작성하고, 내장된 `t2s` 도구가 SQL을 검사한 뒤 읽기 전용으로 실행합니다. [Ossie 지표 정의 파일](tests/fixtures/olist/sales.ossie.yaml)을 연결하면 정의된 지표를 기준으로 답합니다.

## 준비

- Claude Code 또는 Codex
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (첫 실행 시 `t2s` 설치에 사용)
- 데이터베이스 접속 주소와 **읽기 전용 계정**

## 설치

GitHub 저장소 접근 권한이 있는 터미널에서 실행합니다.

### Claude Code

```bash
claude plugin marketplace add zisu17/text2sql-plugin
claude plugin install text2sql@text2sql-plugin
```

### Codex

```bash
codex plugin marketplace add zisu17/text2sql-plugin
codex plugin add text2sql@text2sql-plugin
```

Codex는 첫 사용 전에 일반 터미널에서 `t2s`를 설치합니다. 플러그인을 업데이트한 뒤에도 다시 실행합니다.

```bash
sh ~/.codex/plugins/cache/text2sql-plugin/text2sql/*/bin/t2s setup
```

## 데이터베이스 연결

에이전트를 시작할 터미널에서 환경변수를 설정합니다. 접속 주소에 비밀번호가 있다면 대화창에 붙여 넣지 마세요.

```bash
export DATABASE_URL='sqlite:////absolute/path/data.sqlite'
# export SEMANTIC_MODEL='/absolute/path/sales.ossie.yaml'  # 지표 정의 파일이 있다면 설정
```

`SEMANTIC_MODEL`은 Ossie 형식(`*.ossie.yaml`)입니다. 생략하면 DB 스키마를 직접 조회합니다. 설정하면 집계 질문은 정의된 지표를 사용합니다.

PostgreSQL을 쓴다면 `DATABASE_URL`을 `postgresql+psycopg://reader:비밀번호@host:5432/dbname` 형식으로 설정하고, 플러그인의 Python 환경에 드라이버를 한 번 설치합니다.

```bash
sh /path/to/text2sql-plugin/bin/t2s setup
# 출력된 env 경로를 아래에 사용
uv pip install --python '/path/from/setup/bin/python' 'psycopg[binary]'
```

원격 DB를 사용하는 Codex 실행 환경에는 네트워크 접근도 필요합니다.

## 질문하기

Claude Code에서는 `/text2sql:ask 월별 매출 보여줘`, Codex에서는 `$ask 월별 매출 보여줘`처럼 요청합니다. 첫 질문에서 사용할 DB와 지표 정의 파일을 확인합니다.

SQL 설명·검사, 스키마 탐색, 지표 정의와 오류 진단도 각 스킬로 요청할 수 있습니다.

## 문제가 생기면

- `no_uv` 또는 `setup_failed`: `uv`를 설치하고 위의 `t2s setup`을 일반 터미널에서 다시 실행합니다.
- `no_database`: `DATABASE_URL`이 설정됐는지 확인합니다.
- `db`: 접속 주소, DB 계정 권한, PostgreSQL 드라이버 또는 네트워크 접근을 확인합니다.

## 라이선스

[MIT](LICENSE)
