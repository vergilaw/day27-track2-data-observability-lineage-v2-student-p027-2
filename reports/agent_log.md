# Agent Log

Ghi lại các quyết định quan trọng: **hypothesis của học viên → agent proposal → test/evidence → accept/reject/revise**.

## Phase 1: Contract Validation

- **Hypothesis**: Validator cần type checking + freshness thì hidden test mới pass.
- **Agent Proposal**: `pd.to_numeric` / `pd.to_datetime` để bắt type drift, so `max(updated_at)` với `utcnow()` cho freshness, cộng heuristic "data cũ hơn 2 giờ thì bỏ qua freshness" để không phá test với fixture tĩnh.
- **Test/Evidence**: `pytest tests_public -q` → 10 passed.
- **Decision**: Accept phần type; **revise** phần freshness (xem mục dưới).

## Phase 1b: Revise freshness — heuristic 2 giờ là sai

- **Hypothesis**: Heuristic "cũ hơn 2 giờ ⇒ coi như fixture" làm detector mù trước đúng loại sự cố lab đang dạy. Fault `stale_kb` lệch **-3 giờ** với SLA 60 phút → rơi vào vùng bị bỏ qua.
- **Agent Proposal**: Bỏ ngưỡng 2 giờ. Mặc định dùng **wall clock**; chỉ khi cả batch cũ hơn 24 giờ mới chuyển sang **batch_lag** (đo độ trễ nội bộ: cột freshness trễ bao xa so với timestamp mới nhất mà batch tự khai báo). Cấu hình được qua `freshness.historical_batch_after_hours`.
- **Test/Evidence**: `make reset && python scripts/inject_fault.py stale_kb && make baseline` → `KB contract decision: QUARANTINE`, `freshness:published_at - mode=wall_clock; delay_minutes=190.33; max=60.0`. Trước đó fault này hoàn toàn im lặng. Public test vẫn 10/10 pass. Test hồi quy: `test_stale_batch_fails_freshness_within_the_wall_clock_window`.
- **Decision**: Accept. Giới hạn còn lại (batch cũ đồng đều) được ghi rõ trong `docs/SOLUTION.md` thay vì che giấu.

## Phase 1c: Severity → action + quarantine

- **Hypothesis**: Biết "check nào fail" chưa đủ; pipeline cần biết **phải làm gì**.
- **Agent Proposal**: Mỗi issue mang thêm `action` (critical→block, warning→quarantine, info→warn); `decide_action()` ra quyết định cấp batch; `split_quarantine()` tách dòng lỗi thay vì drop cả batch.
- **Test/Evidence**: `duplicate_pk` → `BLOCK`, quarantine 6/603 dòng. `test_quarantine_isolates_only_bad_rows` giữ đúng 2 dòng sạch trên 4.
- **Decision**: Accept.

## Phase 1d: Great Expectations Suite → Checkpoint → Action

- **Hypothesis**: 4 expectation rời rạc không phải flow production, và không thể chặn pipeline.
- **Agent Proposal**: Sinh `ExpectationSuite` **từ chính contract YAML** (chống drift giữa contract và GX), gắn `ValidationDefinition` + `Checkpoint`, viết custom `SeverityRoutingAction` ghi evidence JSON và trả exit code 1 khi decision = block. Thêm `ExpectTableRowCountToBeBetween` làm sàn volume.
- **Test/Evidence**: `duplicate_pk` → BLOCK kèm sample `[100000, 100001, 100002, ...]`; `volume_drop` → BLOCK do row-count floor; healthy → PASS 20/20 expectation. 4 test trong `tests/test_gx_checkpoint.py`.
- **Decision**: Accept.

## Phase 2: dbt Protection

- **Hypothesis**: Revenue trên dashboard CEO có thể bị thổi phồng nếu dimension khách hàng có nhiều dòng active.
- **Agent Proposal**: Viết dbt **unit test** `duplicate_active_customer_does_not_inflate_revenue` trước, rồi sửa model: collapse dimension về 1 dòng hiện hành bằng `row_number()` trước khi join.
- **Test/Evidence**: Red/green — với join ngây thơ unit test **FAIL**; sau khi dedup, `dbt build` PASS 23/23. Quan trọng: singular test `assert_no_revenue_inflation` **vẫn PASS** ở trạng thái lỗi vì seed hiện tại chỉ có 1 dòng active/customer → chứng minh vì sao cần unit test chứ không chỉ data test.
- **Decision**: Accept. Có sửa model (khác gợi ý "chưa sửa model") vì để test đỏ trong repo sẽ làm `make dbt` hỏng; quá trình đỏ→xanh đã được ghi lại.

## Phase 2b: `stg_customers` crash khi cast `valid_to`

- **Hypothesis**: `nullif(valid_to, '')` giả định seed là VARCHAR.
- **Agent Proposal**: Dùng `try_cast(valid_to as timestamp)`.
- **Test/Evidence**: Trước: `Conversion Error: invalid timestamp field format: ""`. Sau: build sạch. (DuckDB seed loader suy ra kiểu TIMESTAMP nên `nullif` so sánh TIMESTAMP với `''`.)
- **Decision**: Accept.

## Phase 3: Anomaly Detection

- **Hypothesis**: Z-score sai ở 3 chỗ: outlier quá khứ, seasonality cuối tuần, và history phẳng.
- **Agent Proposal**: `auto` chọn baseline theo `same_segment_history` (cùng thứ trong tuần) → MAD; MAD = 0 thì fallback MeanAD rồi so sánh bằng; `known_event` thì im lặng; `trend` thì dùng EWMA; metric dạng count có thêm relative guard.
- **Test/Evidence**: `volume_drop` → robust detector `is_anomaly=True, score=7.55` trong khi **naive z-score `False`, score=2.27**. `test_mad_catches_the_drop_that_an_outlier_hides_from_zscore`, `test_same_weekday_baseline_prevents_a_weekend_false_positive`, `test_constant_history_no_longer_disables_the_detector`.
- **Decision**: Accept.

## Phase 3b: Distribution drift

- **Hypothesis**: `mean_ratio` mù trước thay đổi *hình dạng* phân phối.
- **Agent Proposal**: PSI (bin theo quantile của baseline) + KS 2 mẫu (tự implement, không cần SciPy) + robust median ratio. PSI/KS chỉ dùng khi mỗi phía ≥5 mẫu.
- **Test/Evidence**: `test_shape_shift_with_identical_means_is_detected` — phân phối tách đôi, trung bình lệch <5%, mean ratio ≈ 1.0 nhưng PSI = 10.5 → phát hiện. `test_same_distribution_is_not_flagged` chống false positive.
- **Decision**: Accept.

## Phase 4: Lineage & Blast Radius

- **Hypothesis**: `get_column_downstream` của starter chỉ trả con trực tiếp, nên `raw_orders.amount` dừng ở `stg_orders.amount_usd` và không bao giờ chỉ ra dashboard CEO.
- **Agent Proposal**: BFS transitive an toàn cycle cho cả dataset lẫn column; thêm traversal ngược để tìm root cause; đọc `dbt_project/target/manifest.json` để dựng graph thật; column lineage lấy từ `meta.upstream_columns` khai báo trong `schema.yml` (chịu được rename `amount` → `amount_usd`) cộng name matching; phát OpenLineage RunEvent kèm facet `columnLineage`.
- **Test/Evidence**: `make lineage` → `raw_orders.amount -> stg_orders.amount_usd -> fct_daily_revenue.daily_revenue -> ceo_revenue_dashboard.revenue`. Tests: `test_column_lineage_is_transitive`, `test_column_lineage_handles_cycles_and_diamonds`, `test_column_lineage_is_derived_from_the_dbt_manifest`, `test_openlineage_events_carry_the_column_lineage_facet`.
- **Decision**: Accept. Không thêm `sqlglot` để tự parse SQL — annotation `meta` đủ chính xác cho lab và không thêm dependency.

## Phase 5: SLO / Error Budget

- **Hypothesis**: Starter không bao giờ page. Nhưng page theo *một* cửa sổ thì hoặc quá nhạy (spike 5 phút cũng gọi), hoặc quá chậm (1 ngày mới biết).
- **Agent Proposal**: Multiwindow multi-burn-rate của Google SRE Workbook: 14.4 (1h/5m) → page critical, 6 (6h/30m) → page high, 3 và 1 → ticket. **Cả hai** cửa sổ phải vượt ngưỡng mới page; short cao + long thấp = `transient_spike` (không page); short thấp + long cao = `burn_decaying` (đang hồi phục → ticket).
- **Test/Evidence**: `test_sustained_fast_burn_pages` (20/15 → page critical), `test_transient_spike_does_not_page` (20/0.4 → không page), `test_recovering_incident_does_not_page_but_leaves_a_ticket`, `test_slow_sustained_burn_tickets_instead_of_paging`. `run_baseline` tính burn thật từ `null_rate` lịch sử (cửa sổ 3 ngày vs 14 ngày).
- **Decision**: Accept.

## Phase 5b: RAG / embedding drift

- **Hypothesis**: Check theo norm sẽ mù nếu model mới cũng xuất unit vector — đúng trường hợp đổi embedding model.
- **Agent Proposal**: `detect_embedding_norm_shift` (robust z + PSI/KS + relative shift có so với độ nhiễu vốn có của baseline) cho input là norm; thêm `detect_embedding_shift` so cosine với centroid baseline khi có vector thật; thêm `evaluate_retrieval` (recall@k/MRR).
- **Test/Evidence**: `test_topic_switch_with_identical_norms_needs_the_vector_signal` — mọi vector đều unit length nên check theo norm trả `False`, còn tín hiệu centroid trả `True`. `test_embedding_model_switch_to_unit_norms_is_detected` cho trường hợp baseline phẳng (MAD = 0).
- **Decision**: Accept.

## Phase 6 & 7: Incident Response

- **Hypothesis**: Cần diễn tập quy trình phản ứng sự cố trên fault `volume_drop`.
- **Agent Proposal**: Chạy fault, thu thập evidence từ contract / dbt / anomaly / lineage / SLO, viết incident report, `make reset` rồi verify recovery.
- **Test/Evidence**: Xem `reports/incident_report.md`. Điểm mấu chốt: contract **PASS**, dbt **PASS 23/23**, naive z-score **im lặng** — chỉ detector robust + seasonal bắt được.
- **Decision**: Accept.

## Ghi chú về cách dùng agent

Không nhận nguyên xi output của agent ở 3 chỗ: heuristic freshness 2 giờ (sai về nghiệp vụ),
EWMA tính score trên 0 thay vì trên phân phối residual (false positive với mọi metric tăng
trưởng), và PSI trên mẫu quá nhỏ (mọi batch 3 dòng đều trông như drift). Cả ba đều lộ ra khi
viết test cho trường hợp *không* được báo động, chứ không phải khi test trường hợp báo động.
