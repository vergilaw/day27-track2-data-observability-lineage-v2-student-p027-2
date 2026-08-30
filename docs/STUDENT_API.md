# Stable Student API

Bộ hidden evaluation của giảng viên sẽ import `student_api.py`.

Bạn có thể refactor code bên trong, nhưng giữ các hàm sau và return shape cơ bản.

## 1. `validate_orders(df, contract_path)`

Return: list dictionary.

Mỗi item nên có dạng:

```python
{
  "check": "unique",
  "column": "order_id",
  "severity": "critical",
  "passed": False,
  "details": "..."
}
```

Hidden cases có thể kiểm tra: required/missing columns, null/unique/accepted/range, type drift, freshness và severity.

## 2. `detect_metric(current, history, method="auto", context=None)`

Return:

```python
{
  "is_anomaly": bool,
  "score": float,
  "method": str,
  "reason": str
}
```

`context` có thể chứa:

```python
{
  "metric_name": "row_count",
  "day_of_week": 5,
  "same_segment_history": [...],
  "known_event": None
}
```

`auto` là nơi phù hợp để bổ sung seasonality/robust baseline.

## 3. `detect_distribution(current_values, baseline_values)`

Return ít nhất `is_anomaly`, `score`, `method`, `reason`.

## 4. `slo_status(target, bad_events, total_events)`

Return ít nhất:

- `allowed_bad_rate`
- `actual_bad_rate`
- `burn_rate`
- `remaining_error_budget_fraction`
- `breached`

## 5. `multiwindow_burn(short_window_burn, long_window_burn)`

Return:

```python
{"page": bool, "severity": str, "reason": str, ...}
```

## 6. `downstream_assets(graph, start)`

Return list transitive downstream assets.

## 7. `column_downstream(graph, start)`

Return transitive downstream columns.

## 8. `rag_length_shift(current_texts, baseline_batch_means)`

Return anomaly dictionary.

## 9. `rag_embedding_shift(current_norms, baseline_norms)`

Return anomaly dictionary.

> Hidden tests không yêu cầu một tool cụ thể. Có thể dùng GX/Soda/Elementary/OpenLineage bên trong miễn interface và behavior đúng.

---

## Helper bổ sung (không bắt buộc cho hidden test)

Hidden evaluation chỉ gọi 9 hàm ở trên. Các helper dưới đây được thêm vào cùng module,
không thay đổi signature/return shape của stable API:

| Hàm | Module | Dùng để |
|---|---|---|
| `decide_action(issues)` | `src.contract_validator` | Quyết định cấp batch: `block` / `quarantine` / `warn` / `pass` |
| `split_quarantine(df, contract)` | `src.contract_validator` | Tách `(clean_rows, quarantined_rows)` theo rule cấp dòng |
| `evaluate_freshness(df, contract, now=None)` | `src.contract_validator` | Freshness riêng lẻ, cho phép truyền `now` để test |
| `ewma_detector(...)` | `observability.anomaly` | Baseline theo xu hướng |
| `evaluate_burn_windows({'5m': .., '1h': ..})` | `observability.slo` | Multi-window với >2 cửa sổ |
| `get_upstream_assets` / `get_column_upstream` | `observability.lineage` | Truy ngược tìm root cause |
| `blast_radius(payload, start, column=...)` | `observability.lineage` | Dataset + column + terminal consumers |
| `extract_dbt_dataset_graph` / `extract_dbt_column_graph` | `observability.lineage` | Dựng lineage từ `target/manifest.json` |
| `to_openlineage_events` / `write_openlineage_events` | `observability.lineage` | Phát OpenLineage RunEvent kèm facet `columnLineage` |
| `detect_embedding_shift(vectors, baseline)` | `observability.rag_metrics` | Drift theo cosine với centroid (khi có vector thật) |
| `evaluate_retrieval(retrieved, relevant, k)` | `observability.rag_metrics` | recall@k / precision@k / MRR |
| `run_orders_checkpoint(df, ...)` | `gx.validate_orders` | Chạy full GX Checkpoint, trả payload quyết định |

Mọi return dict của stable API đều **giữ nguyên các key bắt buộc** và chỉ bổ sung key mới
(ví dụ `action` trong contract issue, `psi`/`ks_pvalue` trong distribution result).
