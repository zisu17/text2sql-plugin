---
description: SQL 설명 요청은 explain-sql 스킬로 간다
runs: 1
allowed_tools: [Skill]
---

SELECT c.region, COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.customer_id WHERE o.status <> 'canceled' GROUP BY c.region

이 쿼리 뭐 하는 거야? 조인은 맞게 된 거야?
