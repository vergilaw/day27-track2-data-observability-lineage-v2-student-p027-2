# Incident Report

## Severity
P2 (High)

## Summary
Phát hiện sự sụt giảm khối lượng dữ liệu đột ngột ở bảng `orders`. Chỉ có 150 trên tổng số khoảng 600 dòng dự kiến được nạp vào hệ thống. Việc này gây ra cảnh báo bất thường về số lượng dòng (row-count anomaly) và hệ lụy trực tiếp đến dashboard doanh thu của CEO, dẫn đến con số doanh thu hàng ngày bị giảm sai lệch.

## Detection
- Signal: Hệ thống phát hiện bất thường (`row-count anomaly: True`) sử dụng phương thức `auto:mad` với điểm số dị biệt cao ở mức 5.53.
- First observed time: Ngay sau khi quá trình nạp dữ liệu (batch ingestion) của pipeline chạy xong.

## Root Cause
Lỗi thất bại một phần trong quá trình nạp dữ liệu (được mô phỏng bởi `inject_fault.py volume_drop`) đã khiến pipeline loại bỏ mất khoảng 75% số bản ghi đơn hàng đầu vào, chỉ giữ lại 150 dòng.

## Evidence
1. Chỉ số cơ sở (baseline metric) của `orders rows` báo cáo chỉ có 150 dòng, giảm mạnh so với mức bình thường là khoảng 600 dòng.
2. Detector bắt lỗi bất thường (anomaly detector) đã phất cờ `row-count anomaly` với `score=5.53`.
3. Toàn bộ các bài kiểm tra data contract đều pass (chứng tỏ schema và giá trị dữ liệu vẫn hợp lệ), qua đó khoanh vùng nguyên nhân hoàn toàn là do sự cố về khối lượng/tính đầy đủ của dữ liệu (volume/completeness).

## Blast Radius

```text
raw_orders
-> stg_orders
   -> fct_daily_revenue
      -> ceo_revenue_dashboard
```

## Mitigation
Chạy lại quy trình nạp để nạp lại toàn bộ dữ liệu (full reload) cho bảng `orders` (mô phỏng thông qua lệnh `make reset`), đảm bảo tất cả 600 bản ghi của đợt xử lý (batch) được nạp thành công.

## Recovery
Đã xác minh rằng số lượng dòng (row counts) trở về mức cơ sở dự kiến (600) và hệ thống phát hiện bất thường (anomaly detector) đã tự động gỡ bỏ cảnh báo.

## Verification
- [x] Contract healthy
- [x] dbt tests healthy
- [x] anomaly returned to expected range
- [x] SLO healthy / budget understood
- [x] downstream output verified
