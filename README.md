# text2sql-plugin

Claude Code와 Codex에서 자연어로 데이터베이스를 조회하는 플러그인입니다. 에이전트가 Graph MCP에서 지표·필드·연결을 찾고 SQL을 직접 씁니다. 정의된 지표는 SQL 안에 `AGG(별칭.지표)`로 넣습니다. 내장된 `t2s`는 그 지표를 정의대로 펼치고, 안전 장치를 검사한 뒤 원본 DB에서 읽기 전용으로 실행합니다. 정의와 다르게 쓴 것(승인 안 된 연결, 정의 밖 테이블·컬럼, 승인 전 지표)은 막지 않고 경고로 알립니다.

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
sh ~/.codex/plugins/cache/text2sql-plugin/text2sql/0.6.0/scripts/t2s setup
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

플러그인의 `mcp.json`은 [text2sql-graph-mcp](https://text2sql-graph-mcp-production.up.railway.app/mcp) 원격 서버의 연결 정보입니다. 서버 자체는 Railway에서 실행됩니다. 이 서버는 Neo4j에 게시된 활성 시맨틱 모델을 읽습니다. `t2s`는 같은 서버의 `GET /snapshot`에서 실행용 모델을 받아 메모리에서 컴파일합니다. 이 경로는 MCP 도구가 아니어서 LLM은 모델 전체를 불러올 수 없습니다. 원본 DB의 행은 MCP가 조회하지 않으므로 실제 값 실행에는 별도 `DATABASE_URL`이 필요합니다.

서버는 Railway 서비스의 `MCP_API_TOKEN`을 Bearer 토큰으로 요구합니다. 비밀번호나 토큰을 이 저장소와 대화에 넣지 마세요.

- **Codex**: `mcp.json`에 원격 서버와 Bearer 인증 방식을 선언했습니다. 현재 확인한 Codex 데스크톱 버전에서는 설치된 플러그인의 스킬은 보이지만 `api_key` 인증을 선언한 MCP 서버는 플러그인 상세 화면의 MCP 목록에 나타나지 않습니다. 이 경우 `codex mcp add text2sql-graph --url https://text2sql-graph-mcp-production.up.railway.app/mcp --bearer-token-env-var TEXT2SQL_MCP_TOKEN`으로 Codex 전역 MCP에 등록합니다. `codex mcp get text2sql-graph`로 등록 상태를 확인할 수 있습니다. 이 등록은 플러그인 상세 화면의 MCP 항목을 추가하지 않지만 `graph-metadata` 스킬에서 같은 서버의 도구를 쓸 수 있게 합니다. 도구 호출과 `t2s` 실행 시 Codex 프로세스에서 `TEXT2SQL_MCP_TOKEN` 환경변수에 Railway `MCP_API_TOKEN` 값이 설정돼 있어야 합니다. 값을 저장소나 대화에 입력하지 마세요.
- **Claude Code**: 플러그인을 설치하거나 업데이트한 뒤 `graph_mcp_token` 설정에 Railway `MCP_API_TOKEN`을 입력합니다. 입력값은 Claude Code의 보안 저장소에 보관되며 `.mcp.json`의 인증 헤더에 사용됩니다. `/mcp`에서 `text2sql-graph` 연결 상태를 확인합니다. 이 설정은 Graph MCP 도구에만 쓰입니다. Claude Code는 플러그인 설정값을 Claude가 Bash로 실행하는 명령에 넘기지 않습니다([플러그인 설정 문서](https://code.claude.com/docs/en/plugins-reference)). 그래서 `t2s`가 실행용 모델을 받으려면 Claude Code를 시작하는 셸에도 `TEXT2SQL_MCP_TOKEN` 환경변수에 같은 토큰을 설정해야 합니다. 값을 저장소나 대화에 입력하지 마세요.

연결 후 `graph_list_models`가 활성 모델을 반환하는지 확인합니다. 예를 들어 `$graph-metadata order_lines.revenue 지표의 물리 컬럼 계보 보여줘`처럼 요청합니다. `t2s context`의 `definition.version_id`가 Graph MCP의 활성 `version_id`와 같은지 확인합니다. [Codex 플러그인 인증 설정](https://developers.openai.com/plugins/build/plugins), [Claude Code 플러그인 MCP 설정](https://code.claude.com/docs/en/mcp)을 참고할 수 있습니다.

## 질문과 스킬

Claude Code에서는 `/text2sql:ask 월별 매출 보여줘`, Codex에서는 `$ask 월별 매출 보여줘`처럼 요청합니다. `ask`는 먼저 Graph MCP의 `graph_search_definitions`(관련도 순 전문 검색)로 게시된 지표를 찾습니다. 같은 뜻의 지표가 있으면 SQL에 `AGG(별칭.지표)`로 넣어 `t2s sql`로 계산합니다. 같은 뜻의 지표가 없으면 계산 기준을 먼저 알리고 SQL 집계로 직접 계산합니다. 같은 뜻의 지표가 승인 전이면 그 지표로 계산하고 승인 전이라고 밝힙니다. 정의 밖 테이블·컬럼이 필요하면 `t2s run`으로 조회하고 답에 밝힙니다. 계산에는 Graph MCP의 활성 모델과 원본 DB의 읽기 전용 접속이 필요합니다.

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
| `context` | SQL 문법·스키마·정의 번호·정의에 든 테이블을 줍니다(정의 밖 테이블은 `schema`가 보여 줍니다). `--datasets a,b`를 붙이면 그 데이터셋 범위의 정의 요약(`catalog`)도 줍니다 |
| `schema`, `values` | DB의 모든 테이블과 컬럼(정의 밖은 `outside_definition`에 따로 적음), 필드의 실제 값을 봅니다. `values`는 `dataset.필드`, 정의 밖 컬럼이면 `dataset.컬럼`이나 `테이블.컬럼`으로 부릅니다 |
| `compile` | Ossie 3층 지표 질의를 SQL로 만듭니다. `--run`을 붙이면 실행합니다. `ask`는 `AGG()`로 쓸 수 없는 모델 수준 지표에만 씁니다 |
| `sql` | Ossie 2층 SQL을 실행합니다. `AGG(별칭.지표)`는 정의대로 펼치고 SQL 집계는 그대로 계산합니다. `ask`의 기본 경로입니다 |
| `validate`, `run` | 실제 테이블에 쓴 SQL을 검사하거나 검사 후 읽기 전용으로 실행합니다. 집계도 됩니다 |
| `check` | 정의를 정적으로 검사하거나 DB와 대조합니다 |

예를 들어 다음 두 명령은 정의된 지표로 고객별 주문 수를 계산합니다. `ask`는 두 번째처럼 SQL을 직접 쓰고 지표를 `AGG()`로 넣는 방식을 기본으로 씁니다.

```bash
sh scripts/t2s compile order_count --by orders.customer_id --run
sh scripts/t2s sql 'SELECT o.customer_id, AGG(o.order_count) AS n FROM orders AS o GROUP BY o.customer_id'
```

`context`는 정의 전체 요약을 내지 않습니다. 에이전트는 Graph MCP 도구(`graph_search_definitions`, `graph_get_dataset`, `graph_find_join_path`, `graph_list_join_rules`)로 필요한 데이터셋을 먼저 좁힙니다. 그다음 `sh scripts/t2s context --datasets orders,customers`처럼 그 범위의 요약만 받습니다. 요약에는 범위 안 데이터셋을 읽는 승인 지표, 그 데이터셋의 필드, 양 끝이 범위 안인 연결, 표(기본키와 측정)가 들어 있습니다. Olist 기준으로 `context` 기본 출력은 약 1만 6천 자에서 약 400자로 줄었습니다. `--datasets orders`를 지정하면 약 4천 자입니다.

`sql`은 조인으로 행이 늘어나도 지표의 기준 행을 한 번만 셉니다. 정의되지 않은 `SUM`·`COUNT` 같은 SQL 집계는 결과의 `undefined`와 `notes`에 표시합니다. `compile --query '{"dimensions": [...], "metrics": [...], "filter": "..."}'`로 Ossie 질의 JSON을 전달할 수 있습니다. `--strict`를 붙이면 3층 규격 밖 계산을 거절하고 Ossie 오류 코드를 반환합니다.

지표 작성 중에는 `context`, `schema`, `values`, `compile`에 `--definition <파일>`을 붙여 로컬 정의를 사용합니다. `compile`의 로컬 결과에는 `preview: true`와 `source.kind: local`이 표시됩니다. 승인 전 지표를 미리 계산하려면 `--allow-draft`도 붙입니다.

`t2s context --local`은 Graph 접속 없이 DB 방언, 스키마, 최대 행 수, 읽기 전용 여부를 표시합니다. `run`, `sql`, `compile --run`에 `--compact`를 붙이면 실행 결과에서 반복되는 SQL과 진단이 줄어듭니다.

```bash
sh scripts/t2s compile avg_delivery_days --allow-draft --run --definition ./sales.ossie.yaml
```

로컬 승인 상태는 정의 저장소에서 검토하고 Graph에 게시한 뒤 일반 조회에 반영됩니다.

## 실행 보호와 점검

`run`, `compile --run`, `sql`은 실행 전에 SQL을 검사합니다. 막는 것은 안전 장치뿐입니다: 데이터 변경·여러 문장·위험 함수·FROM 절 테이블 함수, DB에 없는 테이블·컬럼, 행 수와 시간 제한입니다. 정의와 다르게 쓴 것은 실행하고 결과의 `warnings`로 알립니다. 에이전트는 이 경고를 답의 "정의 밖 사용" 줄에 밝힙니다.

| 경고 | 뜻 |
|---|---|
| `unapproved_join` | 승인된 연결이 아닌 조인(승인 전 연결, 정의에 없는 연결, 키 등식이 아닌 조인). 하위 쿼리로 잇는 조건도 봅니다 |
| `outside_definition` | 정의에 없는 테이블이나 필드로 없는 컬럼을 읽음 |
| `undefined_calculation` | `t2s run`에서 정의된 지표가 아닌 SQL 집계를 계산함(`t2s sql`은 `notes`에 적음) |
| `draft` | 승인 전 지표·연결로 계산함. 승인된 정의만으로 안 될 때 승인 전 지표를 먼저, 그래도 안 되면 승인 전 연결까지 넣습니다 |
| `fan_out`, `date_literal` | 조인으로 값이 부풀 수 있음, 시각 컬럼을 날짜 리터럴과 닫힌 구간으로 비교함 |

SQLite에서는 실행 계획상 큰 표를 반복해서 읽는 질의를 `slow_plan`으로 중단합니다. `--force`로 실행할 수 있으며 시간 제한은 그대로 적용됩니다. 이 실행 계획 검사는 현재 SQLite에만 적용됩니다.

게시 전 정의 소스 파일을 점검하려면 `sh scripts/t2s check <파일> --static` 또는 DB 연결 후 `sh scripts/t2s check <파일>`을 실행합니다. `semantic-sync` 스킬은 결과와 정의에 빠진 테이블·컬럼을 설명합니다.

## 개발과 업로드 파일

```bash
uv run pytest -q
python3 scripts/build_plugin.py
```

`scripts/t2s`는 의존성을 `requirements.lock`의 버전 그대로 설치합니다. 이 파일은 `uv.lock`에서 만든 실행용 목록입니다. 의존성을 바꾸거나 `uv lock`으로 `uv.lock`을 갱신했으면 아래 명령으로 다시 만들고 두 파일을 함께 커밋합니다. 두 파일이 어긋나면 `tests/test_package.py`가 실패합니다.

```bash
uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.lock
```

두 번째 명령(`build_plugin.py`)은 Claude 업로드용 `dist/text2sql.plugin`을 만듭니다. 빌드 스크립트는 개발용 파일과 링크를 제외합니다. Ossie 공식 적합성 사례는 외부 저장소와 DuckDB를 준비한 뒤 `OSSIE_DIR=/path/to/ossie uv run --with duckdb pytest tests/test_ossie_compliance.py`로 실행합니다.

## 변경 사항

### 0.6.0 (깨지는 변경)

- `t2s context`의 기본 출력에서 정의 전체 요약(`catalog`)을 뺐습니다. 요약이 필요하면 `--datasets a,b`로 범위를 지정합니다. 질문 원문 인자는 받기만 하고 쓰지 않습니다.
- `t2s`는 실행용 모델을 Graph 서버의 `GET /snapshot`에서 받습니다. text2sql-mcp 0.3.0 이상이 필요합니다. 0.2.0 서버에서는 `/snapshot`이 없어 404 안내와 함께 실패합니다. 배포는 MCP 서버를 먼저, 이 플러그인을 다음에 합니다. 이 변경으로 `mcp` 패키지 의존성이 빠졌습니다.
- SQL은 에이전트가 자유롭게 씁니다. `t2s run`·`t2s validate`·`t2s sql`은 승인되지 않은 연결로 조인해도 막지 않고 `warnings`에 `unapproved_join`으로 알립니다(승인 전 연결인지, 정의에 없는 연결인지 포함). `EXISTS`·`IN (SELECT …)`·스칼라 하위 쿼리로 잇는 조건도 대조합니다. 예전에는 `t2s run`에서 이런 조인을 거절했습니다.
- 정의 밖 테이블·컬럼도 조회할 수 있고 `outside_definition`으로 알립니다(`t2s run`, `t2s values`, `t2s schema`. `t2s sql`은 정의 데이터셋의 원본이 아닌 테이블만 FROM에 그대로 받습니다). 없는 테이블·컬럼은 실제 DB 기준으로 판단합니다.
- `t2s run`도 집계를 실행하고 `undefined_calculation`으로 알립니다. 예전에는 행 조회만 받았습니다.
- `ask`의 기본 지표 경로를 `t2s compile`에서 `t2s sql`로 바꿨습니다. 에이전트가 SQL을 쓰고 정의된 지표는 `AGG(별칭.지표)`로 넣습니다. `t2s compile`은 `AGG()`로 쓸 수 없는 모델 수준 지표에만 씁니다.
- 승인 전 지표·연결도 막지 않습니다. 승인된 정의만으로 계산할 수 없으면 승인 전 지표를 먼저 넣고, 그래도 안 되면 승인 전 연결까지 넣어 다시 계산한 뒤 `draft`로 알립니다. 지표 목록(`t2s compile`을 지표 없이 부름)에도 승인 전 지표가 상태와 함께 나옵니다.
- 막는 것은 안전 장치(읽기 전용, 한 문장, 위험 함수, 행 수·시간 제한, 느린 쿼리)뿐입니다. 민감 필드(`sensitivity`) 처리는 지웠습니다. 보호가 필요한 데이터는 DB 계정 권한으로 막습니다.
- `t2s values`는 `dataset.필드`로 부릅니다. 정의에 필드로 없는 컬럼은 `dataset.컬럼`이나 `테이블.컬럼`으로 보고 `outside_definition`으로 알립니다. `t2s context`의 `tables`는 정의에 든 테이블이고, 정의 밖 테이블은 `t2s schema`가 보여 줍니다.
- `scripts/t2s`는 의존성을 `requirements.lock`의 버전 그대로 설치합니다. DB 클라이언트 훅은 절대 경로 호출과 `pgcli`·`mycli`·`clickhouse-client`도 묻습니다.

## 관련 문서

| 문서 | 내용 |
| --- | --- |
| [실패 유형](references/failure-types.md) | `diagnose`가 사용하는 실패 분류와 수정 방향 |
| [스킬별 사용법](skills/ask/SKILL.md) | 자연어 조회부터 시작하는 스킬 지침. 다른 스킬은 [skills/](skills/)에서 찾습니다 |
| [MCP 조회 계약](https://github.com/zisu17/text2sql-mcp/blob/main/docs/graphdb-mcp-design.md) | 정의 검색(BM25)·조인 경로·계보 조회 도구와 스냅샷 경로 |
| [시맨틱 모델 작성 규칙](https://github.com/zisu17/semantic-definitions/blob/main/docs/semantic-model.md) | Ossie 정의의 정본·승인 상태·검증 |

## 라이선스

[MIT](LICENSE)
