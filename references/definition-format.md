# 정의 파일 작성 형식 (Ossie 0.3.0.dev0)

define-metric 스킬이 정의 파일에 지표·필드·데이터셋·연결을 적을 때 따르는 형식이다. `t2s check`로 정의를 검사한다.

## 필드와 데이터셋

- 컬럼은 필드로 먼저 추가한다: `datasets[].fields[]` 에 `name`, `expression.sql`, `datatype`, `description`. 테이블이 없으면 데이터셋(`name`, `source`, `primary_key`, `description`, `fields`)부터 추가한다. 식·조건·연결에는 필드 이름만 쓴다.
- `datatype` 은 String, Integer, Decimal, Float, Boolean, Date, Time, DateTime, DateTimeTz 중 하나다.

## 데이터셋 지표

- 지표는 데이터셋의 `metrics` 에 두고 식에 필드 이름만 쓴다. 지표 식에 다른 지표 이름을 쓰지 않는다.
- 빼야 할 행은 식 안의 `FILTER (WHERE ...)` 로 쓴다. 조건에 `AND`·`OR` 를 쓸 수 있다. 날짜 차이는 `DATEDIFF(day, 시작, 끝)` 처럼 단위를 따옴표 없이 쓴다.

## 계산용 데이터셋 (다른 데이터셋의 값이 필요할 때)

다른 데이터셋의 값이 필요하면(예: 주문 상태로 거르는 상품 매출) 계산용 데이터셋을 만들어 그 `metrics` 에 둔다. Ossie 2·3층은 집계 하나가 데이터셋 둘 이상을 읽는 모델 지표를 계산하지 않는다(정의 파일의 `order_lines` 가 예다).

- `source` 에 SELECT 문 하나를 적는다. 기준 표에 필요한 표를 `LEFT JOIN` 으로 N:1 로만 이어 행 수가 기준 표와 같게 한다. 이 정의의 다른 데이터셋이 이름으로 가리키는 테이블만 읽는다. 하위 쿼리·WITH·윈도 함수는 쓰지 않는다.
- `primary_key` 는 기준 표의 기본키다. 기준 표의 같은 행에 잇는 연결(기본키끼리) 하나를 둔다. 그러면 원래 표의 묶음 기준·조건을 그대로 쓴다.
- 붙인 필드의 `description` 에 "계산용 사본. 묶거나 거를 때는 <원래 dataset.field> 를 쓴다"를 적는다.

## 모델 지표

모델 `metrics` 에는 데이터셋 지표끼리 계산한 뒤 합치는 지표만 둔다. 집계 하나가 데이터셋 하나만 읽게 쓴다(예: `SUM(orders.amount) / COUNT(DISTINCT customers.customer_id)`). 집계마다 따로 계산한 뒤 나눈다.

## 연결

새 연결은 `from` 이 여러 행 쪽, `to` 가 한 행 쪽이고 `to_columns` 는 `to` 데이터셋의 기본키여야 한다. 상태는 연결의 확장 칸 `data` 에 `status: draft` 로 적는다.

## 지표 설명 칸

지표마다 다음을 적는다.

- `description`: 한 문장
- `ai_context.synonyms`
- `ai_context.instructions`: 누가 무슨 결정에 쓰는지, 보는 단위, 기간을 자를 날짜 필드, 남은 미정 사항
- `ai_context.examples`: 보는 단위로 묶은 질문 한두 개

## 확장 칸 (TEXT2SQL)

지표의 `nonportable_extensions` 에 `vendor_name: TEXT2SQL` 항목 하나를 두고, `data` 는 YAML 객체로 적는다. JSON 문자열 `data` 와 옛 이름 `custom_extensions` 는 새로 쓰지 않는다. 날짜 값은 따옴표로 감싸 문자열로 적는다(날짜 객체는 Ossie 규격 오류).

```yaml
nonportable_extensions:
  - vendor_name: TEXT2SQL
    data:
      status: draft
      decision: <결정 id>
      discovery:
        - q: 한 건의 단위
          a: 주문. 상품이 여러 개여도 한 번 센다
          by: <답한 사람의 역할>
          at: '<오늘 YYYY-MM-DD>'
      expected:
        value: 0.12
        where: ["<dataset.field 조건>"]
        source: <출처>
```

- `discovery` 에는 질문 키와 확정 답만 적는다. 대화 원문이나 사람 이름은 넣지 않고, 답한 사람은 역할로 적는다.
- `expected` 는 담당자가 아는 값이 있을 때만 적는다.
- 승인할 때는 같은 `data` 에 `status: approved` 와 `approval: {by: <승인한 사람의 역할>, at: "<오늘 YYYY-MM-DD>"}` 를 함께 적는다.

## 결정 목록

새 결정이면 모델의 TEXT2SQL `data` 안 `decisions` 목록에 `{"id", "decider", "decision", "action", "grain", "baseline"}` 하나를 더한다. 같은 `data` 객체에 있는 `revision` 같은 다른 키는 그대로 둔다.

## 개념 매핑

3단계에서 고른 개념 연결은 정의 파일 옆 매핑 파일(`<주제>.mapping.yaml`)의 `concept_mappings` 에 적는다. 그 개념의 `object_mappings` 의 `referent_mappings`, 또는 `link_mappings` 의 `children` 에 `relationship` 과 `expression: <dataset.field>` 를 더한다. "새 개념"이면 온톨로지 파일(`<주제>.ontology.yaml`)에 `concept`·`description`·`type`·`relationships` 를 먼저 더한다. 계산용 데이터셋의 사본 필드는 잇지 않는다.
