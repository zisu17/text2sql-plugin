# text2sql-plugin

자연어 질문에 SQL 로 답하는 Text-to-SQL 플러그인입니다. Claude Code 와 Codex 에서 씁니다. 이 저장소가 곧 플러그인 하나입니다.

숫자를 만드는 일은 명령줄 도구 `t2s` 가 합니다. 지표 SQL 만들기, SQL 검사, 읽기 전용 실행이 여기에 들어갑니다. `t2s` 는 LLM 을 쓰지 않습니다. LLM 은 질문을 해석하고, 길을 고르고, 답을 씁니다. `t2s` 는 플러그인 안에 같이 들어 있습니다.

## 준비물

- [uv](https://docs.astral.sh/uv/getting-started/installation/). 파이썬 프로그램 설치 도구입니다. `t2s` 를 처음 설치할 때 한 번만 씁니다.
- Claude Code 또는 Codex.
- DB 접속 주소(`DATABASE_URL`). 읽기 전용 계정을 씁니다.
- 지표 정의 파일(`SEMANTIC_MODEL`). 없어도 되지만, 그러면 DB 표를 그대로 보고 SQL 을 씁니다.

## 설치

아래 `<owner>/text2sql-plugin` 자리에는 GitHub 저장소 주소를 넣습니다. 내려받은 폴더 경로를 넣어도 됩니다.

### Claude Code

```bash
claude plugin marketplace add <owner>/text2sql-plugin
claude plugin install text2sql@text2sql-plugin
```

설치 없이 써 보려면 내려받은 폴더에서 `claude --plugin-dir .` 로 엽니다. 스킬이 `t2s` 를 처음 부를 때 `t2s` 가 스스로 설치됩니다(인터넷 필요, 몇 초).

### Codex

```bash
codex plugin marketplace add <owner>/text2sql-plugin
codex plugin add text2sql@text2sql-plugin
```

Codex 의 기본 샌드박스는 인터넷과 홈 폴더 쓰기를 막습니다. 그래서 스킬이 `t2s` 를 처음 설치하지 못합니다. **처음 한 번은 보통 터미널에서** 설치해 둡니다.

```bash
sh ~/.codex/plugins/cache/text2sql-plugin/text2sql/*/bin/t2s setup
```

설치가 끝나면 쓰기 금지 샌드박스(`-s read-only`)에서도 돕니다. 플러그인을 새 버전으로 바꾸면 이 명령을 다시 한 번 실행합니다.

Codex 는 처음 실행할 때 플러그인의 훅을 검토할지 묻습니다. 신뢰하면 정의 파일 검사 훅이 돌고, 신뢰하지 않아도 스킬은 그대로 돕니다.

## 처음 써 보기

테스트용 작은 DB(주문 4건)와 예시 정의 파일로 먼저 돌려 봅니다. 저장소를 내려받은 폴더에서 실행합니다.

```bash
mkdir -p data
uv run python -c "from tests.conftest import make_db; make_db('data/demo.sqlite')"
export DATABASE_URL=sqlite:///data/demo.sqlite
export SEMANTIC_MODEL=tests/fixtures/olist/sales.ossie.yaml
claude --plugin-dir .
```

그다음 `/text2sql:ask 월별 매출 보여줘` 처럼 묻습니다. Codex 에서는 `$ask 월별 매출 보여줘` 로 묻습니다.

스킬은 처음 `t2s` 를 부르기 전에 DB 와 정의 파일을 어느 것으로 할지 묻습니다. 위에서 정한 값을 고르면 됩니다.

## DB 와 정의 파일 정하기

값은 환경변수로 정합니다. 비밀번호가 든 접속 주소는 대화창에 붙여 넣지 말고 셸에서 설정합니다.

```bash
export DATABASE_URL='postgresql+psycopg://reader:비밀번호@host:5432/dbname'
export SEMANTIC_MODEL=/path/to/my.ossie.yaml
```

| 이름 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | 필수 | SQLAlchemy 접속 주소. 읽기 전용 계정을 쓴다. SQLite 는 `sqlite:////절대/경로/파일.db` 처럼 쓴다. `sqlite:///data/demo.sqlite` 같은 상대 경로는 실행하는 폴더 기준이다. 파일이 없으면 오류가 난다 |
| `SEMANTIC_MODEL` | 없음 | 지표 정의 파일. 있으면 집계는 지표 정의로만 한다. 지금은 Ossie 형식(`*.ossie.yaml`)을 읽는다. 예시는 `tests/fixtures/olist/sales.ossie.yaml` |
| `DB_SCHEMA` | 없음 | 조회할 스키마(DB 안의 표 묶음) 이름 |
| `SQL_DIALECT` | 드라이버에서 추론 | SQL 문법 이름(sqlglot 기준)을 직접 지정 |
| `MAX_ROWS` | 100 | 결과 행 수 상한 |
| `QUERY_TIMEOUT` | 30 | SQL 실행 시간 제한(초). PostgreSQL·SQLite 에 적용 |
| `T2S_HOME` | `~/.local/share/text2sql-plugin` | `t2s` 가 설치되는 폴더. 지워도 다음 실행 때 다시 만든다 |
| `T2S_LOG` | 없음 | 파일 경로를 주면 명령마다 한 줄 기록을 남긴다. 결과 행은 남기지 않는다 |

### PostgreSQL

`t2s` 에는 SQLite 만 바로 쓸 수 있게 들어 있습니다. PostgreSQL 은 드라이버를 `t2s` 환경에 더합니다. `t2s setup` 이 알려 주는 `env` 폴더를 씁니다.

```bash
sh <플러그인 폴더>/bin/t2s setup
# {"ok": true, "env": "<env 폴더>"}
uv pip install --python "<env 폴더>/bin/python" 'psycopg[binary]'
```

접속 주소는 `postgresql+psycopg://` 로 시작해야 이 드라이버를 씁니다. 플러그인을 새 버전으로 바꾸면 `env` 폴더가 바뀌므로 다시 설치합니다.

## 스킬

| 스킬 | 하는 일 |
|---|---|
| `ask` | 질문에 답한다. 집계는 지표 정의로, 행 조회는 검사한 SELECT 로 실행한다 |
| `explore-schema` | 질문에 관련된 지표·컬럼·테이블 연결과 실제 값을 보여 준다 |
| `explain-sql` | SQL 이 무엇을 하는지 설명한다. 실행하지 않는다 |
| `validate-sql` | SQL 을 실행하지 않고 검사한다 |
| `diagnose` | 틀린 SQL 이나 답의 원인을 실패 유형(F1~F10)으로 찾는다 |
| `define-metric` | 정의에 없는 지표를 묻고 답하며 만들어 정의 파일에 적는다 |
| `semantic-sync` | 정의 파일과 실제 DB 가 맞는지 점검한다. 사용자가 직접 부를 때만 돈다 |

Claude Code 는 `/text2sql:ask`, Codex 는 `$ask` 처럼 부릅니다. 그냥 질문해도 맞는 스킬이 골라집니다. `agents/sql-reviewer.md`(SQL 리뷰 에이전트)는 Claude Code 에서만 돕니다. Codex 에서는 `ask` 스킬이 같은 기준으로 직접 검토합니다.

### 묻지 않고 실행하기

답할 사람이 없을 때는 프롬프트에 출처가 정해졌다고 적습니다.

```bash
claude -p --plugin-dir . --allowedTools "Bash(t2s:*)" --append-system-prompt "데이터 출처는 DATABASE_URL·SEMANTIC_MODEL 환경변수 값으로 확인됐다" "/text2sql:ask 월별 매출 보여줘"
```

```bash
codex exec -s read-only -c shell_environment_policy.inherit=all 'text2sql 플러그인의 $ask 스킬을 써서 답해줘. 데이터 출처는 DATABASE_URL·SEMANTIC_MODEL 환경변수 값으로 확인됐다: 월별 매출 보여줘'
```

`shell_environment_policy.inherit=all` 은 환경변수를 Codex 셸에 넘깁니다. Codex 기본 샌드박스는 네트워크를 막으므로 원격 DB 는 설정에서 열어야 합니다.

## `t2s` 명령

스킬이 부르는 명령입니다. 직접 불러도 됩니다. 출력은 늘 JSON 하나입니다.

| 명령 | 하는 일 |
|---|---|
| `t2s setup` | 설치만 하고 끝낸다 |
| `t2s context` | SQL 문법, 정의 요약, 조회할 수 있는 테이블 |
| `t2s schema [테이블 ...]` | 테이블 목록, 테이블을 주면 컬럼 설명 |
| `t2s values <dataset.field \| table.column>` | 컬럼의 실제 값이나 날짜 범위 |
| `t2s compile <지표 ...> [--by --where --run ...]` | 지표 정의로 SQL 을 만들고, `--run` 이면 실행 |
| `t2s validate <sql \| ->` | 실행하지 않고 검사 |
| `t2s run <sql \| ->` | 검사한 뒤 읽기 전용으로 실행 |
| `t2s check [정의 파일] [--static]` | 정의 파일 검사. `--static` 은 DB 없이 |

`-` 를 주면 SQL 을 표준 입력에서 읽습니다.

## 문제가 생기면

`t2s` 는 오류를 `{"error": {"kind": ..., "message": ...}}` 로 돌려줍니다. `kind` 로 찾아봅니다.

| kind | 뜻 | 할 일 |
|---|---|---|
| `no_uv` | uv 가 없다 | uv 를 설치한다 |
| `setup_failed` | 처음 설치에 실패했다 | 인터넷이 되는 보통 터미널에서 `bin/t2s setup` 을 한 번 실행한다 |
| `no_database` | `DATABASE_URL` 이 없다 | 셸에서 설정한다 |
| `no_definition`, `bad_definition` | 정의 파일이 없거나 읽을 수 없다 | `SEMANTIC_MODEL` 경로와 파일 형식을 확인한다 |
| `bad_args` | `t2s` 명령의 인자가 틀렸다 | 메시지의 사용법대로 인자를 고친다 |
| `syntax` | SQL 문법 오류 | SQL 을 고친다 |
| `unknown_table`, `unknown_column`, `unknown_join` | 없는 테이블·컬럼이거나 정의에 없는 연결 | SQL 을 고친다 |
| `policy` | 쓰기, 여러 문장, 금지 함수, 지표 정의를 피한 집계 | 같은 SQL 로는 안 된다. 질문을 바꾼다 |
| `missing_object`, `type_mismatch` | DB 가 돌려준 오류 | SQL 을 고친다 |
| `timeout` | `QUERY_TIMEOUT` 을 넘었다 | 범위를 좁혀 다시 묻는다 |
| `db` | 접속 실패(주소 형식, 드라이버 없음, 네트워크)나 그 밖의 DB 오류 | 메시지의 안내를 따른다. 드라이버가 없으면 위 PostgreSQL 절대로 설치한다 |

## 안전장치

- **접속 자체가 읽기 전용입니다.** SQLite 와 PostgreSQL 은 접속할 때 읽기 전용으로 엽니다. 검사를 빠져나간 쓰기도 DB 가 거절합니다. 다른 DB 는 계정 권한에 맡깁니다. 그래서 읽기 전용 계정을 써야 합니다.
- **실행 전에 SQL 을 검사합니다.** 조회 한 문장만 받고, 데이터 변경과 위험한 함수는 거절하고, 행 수 상한을 겁니다.
- **정의 파일이 있으면** 정의에 있는 테이블·필드만 쓰고, 집계는 지표 정의로만 합니다. 지표 규칙(예: 취소 주문 제외)을 SQL 로 피해 가지 못하게 하려는 것입니다.
- **훅**은 정의 파일(`*.ossie.yaml`)을 고치면 정적 검사를 돌리고, `sqlite3`·`psql`·`mysql`·`duckdb` 를 직접 부르면 사용자에게 확인을 받습니다. Codex 는 이 확인을 아직 지원하지 않아 그대로 진행합니다. 훅은 보조 장치일 뿐이고, 실제 보장은 읽기 전용 계정과 위 검사입니다.

## 개발

```bash
uv run pytest -q
claude plugin validate .
claude plugin eval . --ablation none --trust-plugin
```

`pytest` 는 `t2s` 점검, `validate` 는 플러그인 구조 점검, `eval` 은 질문이 맞는 스킬로 가는지 봅니다. 버전을 올릴 때는 `plugin.json`, `.claude-plugin/plugin.json`, `pyproject.toml` 의 `version` 을 같이 올립니다.

## 라이선스

MIT. [LICENSE](LICENSE) 를 봅니다.
