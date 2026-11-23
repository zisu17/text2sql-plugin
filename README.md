# text2sql-plugin

Claude Code와 Codex에서 자연어로 데이터베이스를 조회하는 플러그인입니다. 에이전트가 질문을 해석하고 내장된 `t2s`가 SQL을 만들거나 검사한 뒤 읽기 전용으로 실행합니다. 일반 조회의 지표와 연결 규칙은 원격 Graph MCP의 활성 모델에서 읽습니다.

## 한눈에 보기

| 목적 | 이 문서에서 볼 곳 |
| --- | --- |
| 처음 설치하기 | [준비와 설치](#준비와-설치) → [Graph MCP 연결](#graph-mcp-연결) |
| 질문으로 실제 값을 조회하기 | [데이터베이스 연결](#데이터베이스-연결) → [질문과 스킬](#질문과-스킬) |
| 명령줄에서 SQL을 만들거나 검사하기 | [`t2s` 명령](#t2s-명령) → [실행 보호와 점검](#실행-보호와-점검) |
| 정의를 새로 작성·검사하기 | [시맨틱 정의 저장소](https://github.com/zisu17/semantic-definitions#readme)와 `define-metric`·`semantic-sync` 스킬 |

일반 조회는 Graph MCP의 활성 모델을 사용합니다. `define-metric`은 로컬 Ossie 파일로 지표를 작성하고 게시 전에 미리 계산합니다. 실제 지표 값은 원본 DB에서 읽습니다.

## 준비와 설치

- Claude Code 또는 Codex
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`t2s` 첫 설치에 사용)
- 데이터베이스 접속 주소와 읽기 전용 계정

```bash
# Claude Code
claude plugin marketplace add zisu17/text2sql-plugin
claude plugin install text2sql@text2sql-plugin

# Codex
codex plugin marketplace add zisu17/text2sql-plugin
codex plugin add text2sql@text2sql-plugin
```

Codex에서는 첫 사용 전에 일반 터미널에서 `t2s`를 설치합니다. 플러그인을 업데이트한 뒤에도 다시 실행합니다.

```bash
sh ~/.codex/plugins/cache/text2sql-plugin/text2sql/0.5.1/scripts/t2s setup
```

설치 위치가 다르면 해당 플러그인 폴더의 `scripts/t2s` 경로를 사용합니다. 스킬은 `t2s`가 PATH에 없을 때 이 스크립트를 `sh`로 실행합니다.

## 데이터베이스 연결

에이전트를 시작할 터미널에서 환경변수를 설정합니다. 비밀번호가 든 접속 주소는 대화창에 붙여 넣지 마세요.

```bash
export DATABASE_URL='sqlite:////absolute/path/data.sqlite'
```

일반 조회의 시맨틱 정의는 Graph MCP에서 읽습니다. 실제 값 실행에는 `DATABASE_URL`이 필요합니다. `DB_SCHEMA`, `SQL_DIALECT`, `MAX_ROWS`(기본 100), `QUERY_TIMEOUT`(기본 30초)도 설정할 수 있습니다.

PostgreSQL은 `postgresql+psycopg://reader:비밀번호@host:5432/dbname` 형식의 주소를 사용합니다. `t2s setup`이 출력한 `env` 경로에 드라이버를 설치합니다.

```bash
sh /path/to/text2sql-plugin/scripts/t2s setup
uv pip install --python '/path/from/setup/bin/python' 'psycopg[binary]'
```

원격 DB를 사용하는 Codex 실행 환경에는 네트워크 접근도 필요합니다.

## Graph MCP 연결

플러그인의 `mcp.json`은 [text2sql-graph-mcp](https://text2sql-graph-mcp-production.up.railway.app/mcp) 원격 서버의 연결 정보입니다. 서버 자체는 Railway에서 실행됩니다. 이 서버는 Neo4j에 게시된 활성 시맨틱 모델을 읽습니다. `graph_get_model_snapshot`이 실행용 모델을 전달하고 `t2s`가 이를 메모리에서 컴파일합니다. 원본 DB의 행은 MCP가 조회하지 않으므로 실제 값 실행에는 별도 `DATABASE_URL`이 필요합니다.

서버는 Railway 서비스의 `MCP_API_TOKEN`을 Bearer 토큰으로 요구합니다. 비밀번호나 토큰을 이 저장소와 대화에 넣지 마세요.

- **Codex**: `mcp.json`에 원격 서버와 Bearer 인증 방식을 선언했습니다. 현재 확인한 Codex 데스크톱 버전에서는 설치된 플러그인의 스킬은 보이지만 `api_key` 인증을 선언한 MCP 서버는 플러그인 상세 화면의 MCP 목록에 나타나지 않습니다. 이 경우 `codex mcp add text2sql-graph --url https://text2sql-graph-mcp-production.up.railway.app/mcp --bearer-token-env-var TEXT2SQL_MCP_TOKEN`으로 Codex 전역 MCP에 등록합니다. `codex mcp get text2sql-graph`로 등록 상태를 확인할 수 있습니다. 이 등록은 플러그인 상세 화면의 MCP 항목을 추가하지 않지만 `graph-metadata` 스킬에서 같은 서버의 도구를 쓸 수 있게 합니다. 도구 호출과 `t2s` 실행 시 Codex 프로세스에서 `TEXT2SQL_MCP_TOKEN` 환경변수에 Railway `MCP_API_TOKEN` 값이 설정돼 있어야 합니다. 값을 저장소나 대화에 입력하지 마세요.
- **Claude Code**: 플러그인을 설치하거나 업데이트한 뒤 `graph_mcp_token` 설정에 Railway `MCP_API_TOKEN`을 입력합니다. 입력값은 Claude Code의 보안 저장소에 보관되며 `.mcp.json`의 인증 헤더에 사용됩니다. `/mcp`에서 `text2sql-graph` 연결 상태를 확인합니다.

연결 후 `graph_list_models`가 활성 모델을 반환하는지 확인합니다. 예를 들어 `$graph-metadata order_lines.revenue 지표의 물리 컬럼 계보 보여줘`처럼 요청합니다. `t2s context`의 `definition.version_id`가 Graph MCP의 활성 `version_id`와 같은지 확인합니다. [Codex 플러그인 인증 설정](https://developers.openai.com/plugins/build/plugins), [Claude Code 플러그인 MCP 설정](https://code.claude.com/docs/en/mcp)을 참고할 수 있습니다.

## 질문과 스킬

Claude Code에서는 `/text2sql:ask 월별 매출 보여줘`, Codex에서는 `$ask 월별 매출 보여줘`처럼 요청합니다. `ask`는 먼저 Graph MCP에서 게시된 지표와 승인 상태를 찾습니다. 같은 뜻의 지표가 없고 질문에서 계산 기준을 정할 수 있으면 등록 지표가 없다는 사실과 계산 기준을 먼저 알립니다. 이어서 `t2s sql`로 계산하고 결과와 기준을 보여 줍니다. 같은 뜻의 지표가 승인 전이면 사용 가능한 기준을 설명하고 선택을 확인합니다. 계산에는 Graph MCP의 활성 모델과 원본 DB의 읽기 전용 접속이 필요합니다.

| 스킬 | 역할 |
|---|---|
| `ask` | 지표 집계와 행 조회 질문에 답합니다 |
| `explore-schema` | 관련 테이블·컬럼·지표·연결을 찾습니다 |
| `explain-sql` | SQL을 실행하지 않고 설명합니다 |
| `validate-sql` | SQL을 실행하지 않고 검사합니다 |
| `diagnose` | 오류나 잘못된 답의 원인을 분류합니다 |
| `define-metric` | 지표를 정의하고 미리 계산한 뒤 사용자가 승인한 항목을 로컬 정의에 기록합니다 |
| `semantic-sync` | 정의 파일과 실제 DB를 대조하고 누락 범위를 보고합니다 |
| `graph-metadata` | 게시된 정의의 계보·영향도·조인을 Graph MCP에서 찾습니다 |

## `t2s` 명령

스킬이 호출하는 명령입니다. 출력은 JSON입니다.

| 명령 | 역할 |
|---|---|
| `context`, `schema`, `values` | 정의·스키마·실제 필드 값을 탐색합니다 |
| `compile` | Ossie 3층 지표 질의를 SQL로 만듭니다. `--run`을 붙이면 실행합니다 |
| `sql` | Ossie 2층 SQL에서 승인 지표 `AGG(별칭.지표)`와 SQL 집계를 실행합니다 |
| `validate`, `run` | SQL을 검사하거나 검사 후 읽기 전용으로 실행합니다 |
| `check` | 정의를 정적으로 검사하거나 DB와 대조합니다 |

예를 들어 다음 명령은 정의된 지표로 고객별 주문 수를 계산합니다.

```bash
sh scripts/t2s compile order_count --by orders.customer_id --run
sh scripts/t2s sql 'SELECT o.customer_id, AGG(o.order_count) AS n FROM orders AS o GROUP BY o.customer_id'
```

`sql`은 조인으로 행이 늘어나도 지표의 기준 행을 한 번만 셉니다. 정의되지 않은 `SUM`·`COUNT` 같은 SQL 집계는 결과의 `undefined`와 `notes`에 표시합니다. `compile --query '{"dimensions": [...], "metrics": [...], "filter": "..."}'`로 Ossie 질의 JSON을 전달할 수 있습니다. `--strict`를 붙이면 3층 규격 밖 계산을 거절하고 Ossie 오류 코드를 반환합니다.

지표 작성 중에는 `context`, `schema`, `values`, `compile`에 `--definition <파일>`을 붙여 로컬 정의를 사용합니다. `compile`의 로컬 결과에는 `preview: true`와 `source.kind: local`이 표시됩니다. 승인 전 지표를 미리 계산하려면 `--allow-draft`도 붙입니다.

```bash
sh scripts/t2s compile avg_delivery_days --allow-draft --run --definition ./sales.ossie.yaml
```

로컬 승인 상태는 정의 저장소에서 검토하고 Graph에 게시한 뒤 일반 조회에 반영됩니다.

## 실행 보호와 점검

`run`, `compile --run`, `sql`은 실행 전에 SQL을 검사합니다. SQLite에서는 실행 계획상 큰 표를 반복해서 읽는 질의를 `slow_plan`으로 중단합니다. `--force`로 실행할 수 있으며 시간 제한은 그대로 적용됩니다. 이 실행 계획 검사는 현재 SQLite에만 적용됩니다.

게시 전 정의 소스 파일을 점검하려면 `sh scripts/t2s check <파일> --static` 또는 DB 연결 후 `sh scripts/t2s check <파일>`을 실행합니다. `semantic-sync` 스킬은 결과와 정의에 빠진 테이블·컬럼을 설명합니다.

## 개발과 업로드 파일

```bash
uv run pytest -q
python3 scripts/build_plugin.py
```

두 번째 명령은 Claude 업로드용 `dist/text2sql.plugin`을 만듭니다. 빌드 스크립트는 개발용 파일과 링크를 제외합니다. Ossie 공식 적합성 사례는 외부 저장소와 DuckDB를 준비한 뒤 `OSSIE_DIR=/path/to/ossie uv run --with duckdb pytest tests/test_ossie_compliance.py`로 실행합니다.

## 관련 문서

| 문서 | 내용 |
| --- | --- |
| [실패 유형](references/failure-types.md) | `diagnose`가 사용하는 실패 분류와 수정 방향 |
| [스킬별 사용법](skills/ask/SKILL.md) | 자연어 조회부터 시작하는 스킬 지침. 다른 스킬은 [skills/](skills/)에서 찾습니다 |
| [MCP 조회 계약](https://github.com/zisu17/text2sql-mcp/blob/main/docs/graphdb-mcp-design.md) | 활성 모델 스냅샷과 계보 조회 도구 |
| [시맨틱 모델 작성 규칙](https://github.com/zisu17/semantic-definitions/blob/main/docs/semantic-model.md) | Ossie 정의의 정본·승인 상태·검증 |

## 라이선스

[MIT](LICENSE)
