# Lab 27 — Data Reliability Game Day

**Chủ đề:** Data Observability, Data Contracts, dbt Testing, Anomaly Detection, Lineage, SLO và Incident Response  
**Thời lượng gợi ý:** 120 phút  
**Hình thức:** nhóm 2–4 học viên  
**Chi phí:** $0 — chạy local  
**AI coding agent:** được phép và khuyến khích, nhưng phải verify output.

## 1. Scenario

Bạn là **Data/AI Reliability Team** của một công ty e-commerce. Pipeline vẫn báo `SUCCESS`, nhưng CEO thấy revenue giảm bất thường và Support Agent trả policy refund cũ.

Mục tiêu của nhóm:

> **Detect → Triage → Find Root Cause → Determine Blast Radius → Mitigate → Verify Recovery**

Kiến trúc lab:

```text
orders/customers ----------------------+
                                       |
                                       v
                                Data contracts
                                       |
                                       v
                                  dbt models
                                       |
                         +-------------+-------------+
                         |                           |
                         v                           v
                fct_daily_revenue              CEO dashboard

kb_documents -> validation -> active KB -> RAG/Support Agent

Across the pipeline: metrics -> anomaly -> lineage -> SLO -> incident response
```

## 2. Quick start

Yêu cầu: **Python 3.10–3.13**. Docker không bắt buộc.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

make reset
make baseline
make tests          # pytest tests_public tests -q
```

Verify toàn bộ trong một lệnh:

```bash
make check          # reset -> baseline -> gx -> dbt -> lineage -> tests
```

Chạy dbt:

```bash
make dbt
```

Chạy Great Expectations example:

```bash
make gx
```

Dựng lineage từ dbt manifest (chạy sau `make dbt`):

```bash
make lineage
```

Dashboard:

```bash
make dashboard
```

## 3. Starter code đã có gì?

- Bộ **synthetic sample data** đi kèm, không cần tải dataset ngoài.
- Script tạo lại data lớn hơn: `scripts/generate_data.py`.
- Data contract YAML và validator Python cơ bản.
- Great Expectations example nhỏ để học viên mở rộng thành Suite/Checkpoint/Actions.
- dbt project chạy trên DuckDB, có staging + mart + public tests.
- Z-score anomaly detector cơ bản.
- SLO/error-budget calculator cơ bản.
- Dataset-level lineage graph + BFS downstream traversal.
- Streamlit dashboard tối giản.
- 3 public fault scenarios để tập điều tra.
- 10 public tests để kiểm tra stable interface.

**Quan trọng:** starter code chỉ là baseline. Code cố ý **chưa xử lý hoàn chỉnh** seasonality, robust statistics, type drift, freshness contract, column lineage, multi-window burn rate, full GX Actions, RAG embedding drift… Học viên phải nghiên cứu và nâng cấp.

## 4. Public fault scenarios

```bash
python scripts/inject_fault.py duplicate_pk
python scripts/inject_fault.py volume_drop
python scripts/inject_fault.py stale_kb
```

Sau mỗi scenario:

```bash
make baseline
```

Reset về trạng thái khỏe:

```bash
make reset
```

## 5. Trạng thái hoàn thiện

Chi tiết thiết kế, trade-off và cách verify từng phần: **`docs/SOLUTION.md`**.

| Phần | Trạng thái |
|---|---|
| `src/contract_validator.py` — type, freshness (wall clock + batch lag), severity → action, quarantine | ✅ |
| `gx/validate_orders.py` — Suite sinh từ contract → ValidationDefinition → Checkpoint → severity-routing Action | ✅ |
| `dbt_project/` — 3 unit test, 3 singular test, model chống fan-out của SCD | ✅ |
| `observability/anomaly.py` — MAD + MeanAD fallback, same-weekday baseline, EWMA, known_event | ✅ |
| `observability/distribution.py` — PSI + KS + robust ratio (thay mean ratio) | ✅ |
| `observability/slo.py` — multi-window multi-burn-rate theo SRE Workbook | ✅ |
| `observability/lineage.py` — column lineage transitive, dbt manifest, OpenLineage events | ✅ |
| `observability/rag_metrics.py` — embedding norm/centroid drift, retrieval metrics | ✅ |
| `reports/incident_report.md`, `reports/agent_log.md` | ✅ |
| Phase 6 mystery incident (dataset do giảng viên cung cấp) | ⏳ chờ dataset |

35 regression test trong `tests/` mô tả đúng failure mà starter code bỏ sót
(z-score bị outlier che mắt, MAD = 0, column lineage không transitive, transient spike gây page…).

## 6. Hidden evaluation

Bộ hidden evaluation gồm **20 test cases khó** không nằm trong ZIP học viên. Giảng viên chạy riêng để đánh giá robustness.

Hidden test sẽ gọi stable interface trong `student_api.py`. Nếu refactor code, vẫn cần giữ interface mô tả trong `docs/STUDENT_API.md`.

## 7. Dùng AI coding agent

Có thể dùng Claude Code, Cursor, Codex, ChatGPT, Gemini CLI hoặc agent khác.

Mỗi thay đổi quan trọng cần có:

1. Hypothesis của học viên.
2. Agent proposal.
3. Test/evidence.
4. Quyết định accept/reject/revise.

Ghi ngắn gọn vào `reports/agent_log.md`.

## 8. Tài liệu học tiếp

- Great Expectations Core: https://docs.greatexpectations.io/
- dbt data tests: https://docs.getdbt.com/docs/build/data-tests
- dbt unit tests: https://docs.getdbt.com/docs/build/unit-tests
- OpenLineage: https://openlineage.io/
- Google SRE Workbook — Alerting on SLOs: https://sre.google/workbook/alerting-on-slos/
- Soda Core: https://docs.soda.io/
- Elementary OSS: https://github.com/elementary-data/elementary

---

**Rule quan trọng nhất:** pipeline `SUCCESS` không có nghĩa data đúng.
