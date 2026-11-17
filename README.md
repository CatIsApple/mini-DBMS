# Mini DBMS (Python)

외부 의존성 없는 순수 Python(3.10+) 미니 DBMS. 세 단계로 구성됩니다.

| Phase | 내용 | 코드 |
|---|---|---|
| 1 | SQL 처리기 (CLI + 파일 저장) | `minidb/parser.py`, `engine.py`, `storage.py`, `cli.py` |
| 2 | B+ 트리 인덱스 + 100만 건 성능 비교 | `minidb/bptree.py`, `bench.py` |
| 3 | Thread Pool API 서버 | `minidb/server.py` |

## 사용법

스키마는 이미 존재한다고 가정하므로 `data/schema.json`에 정의합니다 (`id`는 자동 부여되는 암묵적 INT 컬럼).

```json
{ "users": { "name": "TEXT", "age": "INT", "email": "TEXT" } }
```

지원 타입: `INT`(64bit), `FLOAT`, `TEXT`.

```bash
python -m minidb.cli -d data -f examples.sql          # SQL 파일 실행
python -m minidb.cli -d data -e "SELECT * FROM users WHERE id = 2"
python -m minidb.cli -d data                           # 대화형 (stdin 파이프도 가능)
python -m minidb.server -d data --port 8080 --workers 8
python bench.py                                        # 100만 건 벤치마크
python -m unittest discover -s tests -t .              # 테스트 57개
```

### 지원 SQL

```sql
INSERT INTO users (name, age) VALUES ('a', 1), ('b', 2);   -- 다중 행, id 자동 부여, 전체-또는-전무
SELECT * | col, ... | COUNT(*) FROM users
  [WHERE a = 1 AND (b < 'x' OR c IS NOT NULL)] [LIMIT n];  -- = != <> < <= > >=, AND/OR/괄호, IS [NOT] NULL
EXPLAIN SELECT * FROM users WHERE id = 5;                  -- 실행 계획 (INDEX LOOKUP / INDEX RANGE / FULL TABLE SCAN)
```

`--` 주석, 여러 문장(`;`), 문자열 내 `''` 이스케이프를 지원합니다. 스크립트는 **전체를 먼저 파싱**하므로 문법 오류가 있으면 아무 문장도 실행되지 않습니다.

### API

| 요청 | 응답 |
|---|---|
| `POST /query` `{"sql": "..."}` | `200 {"results":[{kind, columns, rows, plan}|{kind:"insert", affected, first_id, last_id}]}` |
| SQL 오류 | `400 {"error","type","completed"}` (`completed` = 오류 전에 실행된 문장 수) |
| `GET /tables` | 스키마 |
| `GET /health` | 워커 수 / 대기 큐 길이 |
| 큐 포화 | `503` + `Retry-After` |

잘못된 JSON 400, 본문 8MB 초과 413, Content-Length 없음 411, 미지원 메서드 405, 없는 경로 404.

## 설계

```
SQL text → tokenize/parse (AST) → Database._select/_insert → plan → Table (RW lock)
                                                                   ├─ B+ tree: id → byte offset
                                                                   └─ users.tbl: [id,"a",1,...]\n  (append-only JSON lines)
```

- **저장 포맷**: 테이블당 JSON-lines 파일. INSERT는 파일 끝에 append, 인덱스는 `id → 바이트 오프셋`.
- **시작 시**: 파일을 훑어 B+ 트리 재구축. 크래시로 마지막 줄이 잘려 있으면 버리고 truncate (복구).
- **B+ 트리**: order 64, 리프 연결 리스트로 범위 스캔. 검증용 `check()`로 불변식(정렬·깊이 동일·경계) 테스트.
- **플래너**: 최상위 `AND` 항 중 `id = v`는 점 조회, `id <,<=,>,>=`는 범위 스캔. 그 외(`OR`, `!=`, 다른 컬럼)는 풀스캔. 인덱스를 쓰더라도 전체 WHERE를 다시 적용하므로 결과가 항상 정확합니다.
- **NULL**: SQL 의미론 (`= NULL`, `!= x` 는 NULL 행을 제외; `IS NULL` 사용).
- **동시성**: 테이블별 writer-preferring RWLock. 읽기는 스레드별 파일 핸들로 병렬, 쓰기(id 할당 + append + 인덱스 갱신)는 배타적 → id 중복/인덱스 불일치 없음.
- **서버**: accept 스레드 → 유한 큐 → N개 워커(직접 구현한 Thread Pool). 큐가 가득 차면 즉시 503(백프레셔). HTTP/1.0(keep-alive 없음)과 10초 소켓 타임아웃으로 느린 클라이언트가 워커를 점유하지 못하게 함.

## 성능 (Windows 11, Python 3.11, 100만 행)

| 항목 | 결과 |
|---|---|
| INSERT (SQL 경유, 1000행/문장) | 14.9s (약 67,000 rows/s) |
| 재시작 시 인덱스 재구축 | 1.6s |
| `WHERE id = ?` (B+ 트리, 트리 높이 4) | **56 µs/query** |
| `WHERE email = ?` (선형 탐색) | **1,917 ms/query** |
| 속도 차이 | 약 **34,000배** |
| `id` 범위 1만 건 | 25 ms |

## 알려진 한계 (의도적 단순화)

- UPDATE / DELETE / CREATE TABLE 미구현 (B+ 트리도 insert-only). 요구사항 범위 밖.
- `OR` 조건과 비-id 컬럼에는 인덱스를 쓰지 않음 (2차 인덱스 없음).
- 인덱스는 메모리 기반이며 시작 시 파일에서 재구축. 쓰기 도중 디스크 가득 참 같은 I/O 오류는 롤백하지 않고 다음 open 시 잘린 줄만 복구.
- 기본은 `flush`만 수행 (OS 크래시 내구성은 `--fsync` 옵션으로).
- 하나의 `data/` 디렉터리를 여러 프로세스가 동시에 열면 안 됩니다 (프로세스 간 락 없음).
