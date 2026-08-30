# Solution Notes — Data Reliability Game Day

Tài liệu này ghi lại **những gì đã build thêm trên starter code**, lý do thiết kế, và
lệnh để verify lại từng phần. Nguyên tắc xuyên suốt: mỗi thay đổi phải bắt được một
failure mà baseline **không** bắt được, và failure đó phải có test chứng minh.

Verify toàn bộ:

```bash
make check      # reset -> baseline -> gx -> dbt -> lineage -> tests
```

---

## Phase 1 — Contract & validation

`src/contract_validator.py`

| Bổ sung | Lý do |
|---|---|
| Type validation (`integer/number/datetime/string/boolean`) | Type drift (`amount = "N/A"`) không vi phạm null/unique/range nào cả |
| `min_length` / `max_length` | KB contract dùng nó để bắt document bị cắt cụt |
| `columns:` **hoặc** `fields:` | `kb_contract.yaml` dùng `fields:`, starter chỉ đọc `columns:` nên bỏ qua toàn bộ contract KB |
| `action` theo severity | critical → `block`, warning → `quarantine`, info → `warn` |
| `decide_action(issues)` | Quyết định cấp batch: block > quarantine > warn > pass |
| `split_quarantine(df, contract)` | Tách row lỗi ra khỏi batch thay vì drop cả batch |
| Freshness 2 chế độ | Xem bên dưới |

### Freshness: vì sao có hai chế độ

Starter (bản trước) bỏ qua freshness khi data cũ hơn 2 giờ, với lý do "chắc là test
fixture". Hệ quả: fault `stale_kb` (lệch **-3 giờ**, SLA 60 phút) **không bị bắt**.

Thiết kế hiện tại:

- **wall_clock** (mặc định): so `max(updated_at)` với thời gian thực. Đây là chế độ
  đúng cho pipeline đang chạy, và nó bắt được `stale_kb`.
- **batch_lag**: chỉ khi cả batch cũ hơn `historical_batch_after_hours` (mặc định 24h).
  Lúc đó wall clock chỉ đo "file này được ghi bao lâu rồi", nên ta chuyển sang đo độ trễ
  *nội bộ* của batch: cột freshness trễ bao xa so với timestamp mới nhất mà chính batch
  khai báo. Nhờ vậy fixture tĩnh (public test) không false-positive, nhưng một slice bị
  cập nhật thiếu vẫn lộ ra.

**Giới hạn đã biết:** batch cũ *đồng đều* (mọi cột đều cũ) sẽ không bị bắt bởi freshness.
Đó là lý do volume/arrival monitoring nằm ở tầng anomaly chứ không phải contract.

### Great Expectations — `gx/validate_orders.py`

Đã nâng từ 4 expectation rời rạc lên đúng shape production:

```text
contracts/orders_contract.yaml
   -> ExpectationSuite (sinh tự động từ contract)
   -> ValidationDefinition
   -> Checkpoint
   -> SeverityRoutingAction  (block / quarantine / warn + ghi evidence JSON)
```

- Suite được **sinh từ contract YAML**, nên contract và GX không thể drift khỏi nhau.
- `SeverityRoutingAction` là custom `ValidationAction`: gom failure theo severity, ra
  quyết định, ghi `reports/gx_validation_result.json` (kèm sample giá trị sai) để incident
  report trích dẫn được.
- `ExpectTableRowCountToBeBetween` với sàn = 50% batch baseline → bắt partial ingestion.
- `main()` trả exit code 1 khi decision = block, để orchestrator thật sự chặn được pipeline.

Evidence:

```bash
python scripts/inject_fault.py duplicate_pk && make gx   # BLOCK, unique fail, sample [100000, ...]
python scripts/inject_fault.py volume_drop  && make gx   # BLOCK, row-count floor
```

---

## Phase 2 — dbt

- `fct_daily_revenue.sql`: dimension khách hàng được **collapse về 1 dòng hiện hành**
  (`row_number()` ưu tiên `valid_to is null`, rồi `valid_from` mới nhất) trước khi join.
  Trước đó, hai dòng `is_active = true` của cùng một customer sẽ fan-out và thổi phồng
  revenue **mà không có lỗi SQL nào**.
- `stg_customers.sql`: `nullif(valid_to, '')` crash khi seed loader suy ra kiểu TIMESTAMP;
  đổi sang `try_cast` để đúng với cả VARCHAR lẫn TIMESTAMP.
- Unit tests (`models/marts/unit_tests.yml`):
  1. `completed_orders_sum_to_expected_revenue` — happy path,
  2. `duplicate_active_customer_does_not_inflate_revenue` — **bug thật**,
  3. `only_completed_orders_count_towards_revenue` — filter status.
- Singular tests: `assert_nonnegative_revenue`, `assert_no_revenue_inflation`,
  `assert_row_counts_match_source` (mới — bắt fan-out ở phía số dòng).

**Red/green evidence** (đã chạy): với join ngây thơ, `duplicate_active_customer_does_not_inflate_revenue`
**FAIL**; sau khi dedup dimension, `dbt build` PASS 23/23.

> Đáng chú ý: singular test `assert_no_revenue_inflation` **vẫn PASS** với model lỗi, vì
> dữ liệu production hiện tại chỉ có 1 dòng active mỗi customer. Đó chính là lý do cần
> dbt **unit test**: nó test *logic transformation* bằng input tự dựng, không phụ thuộc
> việc dữ liệu hôm nay có tình cờ sạch hay không.

---

## Phase 3 — Anomaly detection

`observability/anomaly.py`

| Signal | Bắt được gì mà z-score bỏ sót |
|---|---|
| Median + MAD | Một outlier trong quá khứ làm phồng `std`, che mất cú sập hôm nay |
| Fallback MeanAD khi MAD = 0 | History phẳng ⇒ MAD = 0 ⇒ starter trả "không bất thường" cho **mọi** giá trị |
| Same-weekday baseline | Thứ Bảy giảm 60% là bình thường, không phải sự cố |
| EWMA (khi `context["trend"]`) | Metric tăng trưởng đều: mỗi đỉnh mới không phải là anomaly |
| `known_event` | Black Friday không page ai cả |
| Relative guard cho metric dạng count | Sụt >50% vẫn page kể cả khi history rất nhiễu |

EWMA lag sau chuỗi có xu hướng, nên residual có **bias hệ thống**; score được tính trên
phân phối residual (trừ median residual) chứ không phải trên 0 — nếu không, mọi ngày đều báo động.

`observability/distribution.py` — thay `mean_ratio` bằng **PSI + KS + robust median ratio**.
Mean ratio không thấy gì khi phân phối tách đôi nhưng trung bình giữ nguyên; test
`test_shape_shift_with_identical_means_is_detected` chứng minh điều đó. PSI/KS chỉ được
dùng khi cỡ mẫu đủ (≥5 mỗi phía), nếu không mọi batch 3 dòng đều trông như drift.

---

## Phase 4 — Lineage & blast radius

`observability/lineage.py`

- `get_column_downstream` giờ là **BFS transitive, an toàn với cycle** (starter chỉ trả con trực tiếp).
- `get_upstream_assets` / `get_column_upstream` cho hướng ngược lại khi đi tìm root cause.
- `blast_radius(payload, start, column=...)` gộp dataset + column + terminal consumers.
- `extract_dbt_dataset_graph` / `extract_dbt_column_graph` đọc `dbt_project/target/manifest.json`.
  dbt-core không tự sinh column lineage, nên dùng hai nguồn: `meta.upstream_columns` khai báo
  trong `schema.yml` (chịu được rename `amount` → `amount_usd`) **và** name matching.
- `to_openlineage_events` / `write_openlineage_events` phát RunEvent đúng spec OpenLineage
  1-0-5 kèm facet `columnLineage` — POST vào Marquez được, mà không cần dựng server.

```bash
make dbt && make lineage
# raw_orders.amount -> stg_orders.amount_usd -> fct_daily_revenue.daily_revenue -> ceo_revenue_dashboard.revenue
```

---

## Phase 5 — SLO / error budget

`observability/slo.py` — policy multiwindow multi-burn-rate theo Google SRE Workbook:

| burn | window dài / ngắn | ngân sách tiêu | hành động |
|---:|---|---:|---|
| 14.4 | 1h / 5m | 2% | page (critical) |
| 6 | 6h / 30m | 5% | page (high) |
| 3 | 1d / 2h | 10% | ticket |
| 1 | 3d / 6h | 10% | ticket |

Cả **hai** cửa sổ phải vượt ngưỡng thì mới page:

- short cao + long thấp → `transient_spike`, **không page** (chống alert fatigue),
- short thấp + long cao → `burn_decaying`, incident đang hồi phục → ticket, không page,
- cả hai cao → page.

`calculate_slo` bổ sung `error_budget_events` và `hours_to_exhaustion` (cửa sổ 30 ngày).
`evaluate_burn_windows` xử lý >2 cửa sổ, khớp đúng cặp window của từng tier.

---

## RAG / KB metrics

`observability/rag_metrics.py`

- `detect_embedding_norm_shift`: robust z + PSI/KS + relative median shift, trong đó
  relative shift chỉ tính khi nó **lớn hơn độ nhiễu vốn có của baseline** — nhờ vậy
  baseline phẳng (MAD = 0, ví dụ model chuyển sang output đã chuẩn hoá L2) vẫn bắt được
  mà không false-positive trên nhiễu bình thường.
- `detect_embedding_shift`: khi có vector thật, so cosine với centroid baseline. Bắt được
  **đổi chủ đề / đổi model** ngay cả khi mọi vector đều là unit vector (norm không đổi) —
  đúng trường hợp mà check theo norm mù hoàn toàn (`test_topic_switch_with_identical_norms_needs_the_vector_signal`).
- `detect_text_length_shift`: thêm guard theo tỉ lệ để history phẳng không vô hiệu hoá detector.
- `evaluate_retrieval`: recall@k / precision@k / MRR trên golden set — chứng minh sự cố KB
  là **regression người dùng thấy được**, không chỉ là con số nội bộ.

---

## Đã wire vào đâu

- `scripts/run_baseline.py`: contract + quarantine + anomaly (robust vs naive) + distribution
  drift + KB contract + KB drift + SLO + multi-window burn + blast radius (dataset & column)
  + phát OpenLineage events.
- `scripts/build_lineage.py` (`make lineage`): dựng lineage từ dbt manifest, merge với các
  consumer ngoài dbt (dashboard, RAG chain).
- `dashboard/app.py`: decision batch, error budget, so sánh robust vs naive detector, drift,
  blast radius.
- `tests/`: 35 regression test, mỗi test nêu rõ failure mà starter bỏ sót.

## Giới hạn còn lại

1. Freshness không bắt được batch cũ đồng đều (đã giải thích ở trên).
2. Column lineage phụ thuộc annotation `meta.upstream_columns`; parser SQL (sqlglot) sẽ
   tự động hoá được nhưng thêm dependency.
3. Không có embedding model thật trong lab: `run_baseline` dùng cột lịch sử
   `embedding_norm_mean` làm proxy để chạy hết interface.
4. Fixture `orders.csv` luôn ~600 dòng trong khi history mô phỏng cuối tuần chỉ ~43%.
   Vì vậy chạy `make baseline` vào Thứ Bảy/Chủ Nhật sẽ thấy "volume_spike" — đó là
   artifact của dữ liệu mẫu, `run_baseline` in chú thích rõ khi gặp trường hợp này.
