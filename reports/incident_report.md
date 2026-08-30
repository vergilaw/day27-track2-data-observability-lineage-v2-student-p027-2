# Incident Report — Partial ingestion trên `orders`

## Severity
**P2 (High)** — số liệu revenue trên dashboard CEO sai, nhưng không mất dữ liệu vĩnh viễn và không ảnh hưởng khách hàng trực tiếp.

## Summary
Batch `orders` chỉ nạp được **150/600** dòng (~25%). Pipeline báo `SUCCESS`, toàn bộ data
contract **PASS**, toàn bộ dbt test **PASS 23/23** — nhưng `fct_daily_revenue` và dashboard
CEO báo doanh thu thấp giả tạo. Đây đúng là loại sự cố mà "pipeline xanh" không phát hiện được.

## Detection
- **Signal**: `row-count anomaly = True`, method `auto:mad`, **score 7.55**, baseline `same_segment` (cùng thứ trong tuần, n=6, median=262, MAD=10).
- **Naive z-score bỏ sót**: `is_anomaly = False`, score **2.27** (mean=494.6, std=151.6 — độ lệch chuẩn bị thổi phồng bởi chính seasonality cuối tuần).
- **First observed**: ngay sau batch ingestion, ở lần chạy `make baseline` kế tiếp.
- **Detector nào im lặng** (và vì sao điều đó có ý nghĩa):

  | Lớp kiểm soát | Kết quả | Diễn giải |
  |---|---|---|
  | Data contract (7 cột, 20 check) | PASS | Dòng còn lại **hợp lệ** — không phải lỗi schema/giá trị |
  | GX checkpoint (không có row-count floor) | PASS | Rule tĩnh không biết "thiếu bao nhiêu là thiếu" |
  | dbt tests (23) | PASS | Transformation đúng; input mới là thứ bị thiếu |
  | Distribution drift trên `amount` | stable (psi=0.045, ks_p=0.96) | Phân phối giá trị **không đổi** → loại giả thuyết corruption |
  | Anomaly (robust + seasonal) | **FAIL** | Chỉ tín hiệu thống kê về *volume* bắt được |

## Root Cause
Partial ingestion: job nạp dữ liệu kết thúc sớm và chỉ ghi ~25% số bản ghi đầu vào
(mô phỏng bằng `scripts/inject_fault.py volume_drop`). Không có lỗi nào được ném ra,
nên orchestrator đánh dấu run là thành công.

## Evidence
1. `reports/latest_metrics.json`: `orders_rows = 150`, `row_count_anomaly.score = 7.55`,
   `row_count_anomaly_naive_zscore.is_anomaly = false`.
2. `contract_decision = {"action": "pass", "failed_checks": 0}` — dữ liệu còn lại hợp lệ.
3. `amount_distribution_shift.verdict = "stable"` — median 56.72 so với baseline 57.65;
   loại bỏ giả thuyết "giá trị bị hỏng", chỉ còn giả thuyết "thiếu dòng".
4. `dbt build` PASS 23/23 — loại bỏ giả thuyết lỗi transformation.
5. GX với row-count floor (50% baseline): **BLOCK** — cho thấy guard tĩnh này đáng thêm.

## Blast Radius

Dataset (từ `make lineage`, dựng từ dbt manifest + consumer graph):

```text
raw_orders -> stg_orders -> fct_daily_revenue -> ceo_revenue_dashboard
```

Column-level:

```text
raw_orders.amount -> stg_orders.amount_usd -> fct_daily_revenue.daily_revenue -> ceo_revenue_dashboard.revenue
```

Terminal consumer bị ảnh hưởng: **ceo_revenue_dashboard**. Nhánh KB/RAG
(`kb_documents -> kb_active_docs -> rag_index -> support_agent`) **không** bị ảnh hưởng —
xác nhận bằng lineage chứ không phải bằng suy đoán.

## Mitigation
1. Chặn publish batch: GX checkpoint trả exit code 1 khi decision = `block` (row-count floor).
2. Re-run ingestion ở chế độ full reload cho `orders` (trong lab: `make reset`).
3. Rebuild mart: `make dbt`.

## Recovery
- `orders_rows` trở lại **600**.
- `row_count_anomaly.is_anomaly` trở lại đúng kỳ vọng của baseline cùng-thứ-trong-tuần.
- `dbt build` PASS 23/23, `pytest tests_public tests -q` PASS 45/45.
- `contract_decision = PASS`, quarantine 0 dòng.

## Verification
- [x] Contract healthy (0 failed checks, 0 critical)
- [x] dbt tests healthy (23/23)
- [x] Anomaly trở về vùng kỳ vọng
- [x] SLO healthy — burn rate 0, error budget còn 100%, multi-window `page=False`
- [x] Downstream output verified (`fct_daily_revenue` khớp `assert_row_counts_match_source`)

## Prevention
1. **Row-count floor trong GX checkpoint** (đã thêm): sàn = 50% batch baseline, severity
   critical → chặn pipeline ngay tại cổng ingestion thay vì phát hiện ở dashboard.
2. **Singular test `assert_row_counts_match_source`** (đã thêm): số dòng của mart phải khớp
   số đơn completed của staging — bắt cả fan-out lẫn thiếu dòng.
3. **Seasonal baseline mặc định** cho mọi metric dạng count: so với cùng thứ trong tuần,
   không so với trung bình 14 ngày.
4. **Multi-window burn rate** cho SLI ingestion completeness, để một batch lỗi lẻ không page
   nhưng lỗi kéo dài thì page (`observability/slo.py`).
5. Việc cần làm tiếp (ngoài phạm vi lab): ingestion job phải publish `expected_row_count` để
   so khớp deterministic, thay vì để lớp thống kê đoán.

## Bài học
Ba lớp kiểm soát độc lập (contract, dbt test, distribution drift) đều **xanh** trong lúc số
liệu đưa lên CEO thì **sai**. Deterministic check chỉ trả lời "dữ liệu có mặt có đúng không",
không trả lời "dữ liệu có đủ không" — và z-score ngây thơ trên chuỗi có seasonality thì
tự làm hỏng chính mình vì độ lệch chuẩn bị mùa vụ thổi phồng.
