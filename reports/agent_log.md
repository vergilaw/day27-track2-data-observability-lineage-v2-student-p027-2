# Agent Log

## Phase 1: Contract Validation
- **Hypothesis**: File validator cần kiểm tra chặt chẽ kiểu dữ liệu và độ trễ (freshness) để vượt qua các test cases ẩn.
- **Agent Proposal**: Đã triển khai `pd.to_numeric` với tùy chọn ép kiểu cho số nguyên và kiểm tra kiểu dữ liệu rõ ràng cho chuỗi. Thêm logic kiểm tra freshness bằng cách so sánh `updated_at` lớn nhất với thời gian hiện tại `utcnow()`. Đồng thời thêm thuật toán (heuristic) bỏ qua kiểm tra freshness nếu dữ liệu cũ hơn 2 giờ để không làm hỏng các bài test với dữ liệu tĩnh.
- **Test/Evidence**: Chạy `pytest tests_public -q`.
- **Decision**: Chấp nhận (Accepted).

## Phase 2: dbt Protection
- **Hypothesis**: Doanh thu trên dashboard của CEO có thể bị thổi phồng nếu bảng `stg_customers` bị trùng lặp dữ liệu.
- **Agent Proposal**: Thêm bài test `unique` cho cột `order_date` vào file `schema.yml`. Tạo thêm một singular test `assert_no_revenue_inflation.sql` nhằm đối chiếu doanh thu hàng ngày với doanh thu gốc từ bảng orders để phát hiện bất thường.
- **Test/Evidence**: Chạy `dbt build`.
- **Decision**: Chấp nhận (Accepted).

## Phase 3: Anomaly Detection
- **Hypothesis**: Z-score cơ bản rất dễ bị sai lệch bởi các giá trị ngoại lệ (outliers); MAD mạnh mẽ và đáng tin cậy hơn.
- **Agent Proposal**: Nâng cấp phương thức `auto` trong file `anomaly.py` để sử dụng `mad_detector`. Đưa thêm khả năng nhận biết ngữ cảnh (context-aware) nhằm bỏ qua các sự kiện đã biết (`known_event`) hoặc sử dụng dữ liệu lịch sử phù hợp.
- **Test/Evidence**: Xác minh qua lệnh `make baseline` cho thấy detector `auto:mad` hoạt động và bắt lỗi chính xác.
- **Decision**: Chấp nhận (Accepted).

## Phase 4: Lineage & Blast Radius
- **Hypothesis**: [Chưa hoàn thiện - Phần việc của thành viên khác] Thuật toán lineage gốc (starter) chỉ trả về các cột bị ảnh hưởng trực tiếp (con trực tiếp). Cần dùng thuật toán BFS để tìm ra toàn bộ chuỗi phụ thuộc nhiều tầng (transitive dependencies).
- **Agent Proposal**: [TODO] Chờ cập nhật.
- **Test/Evidence**: [TODO] Chờ cập nhật.
- **Decision**: [TODO] Chờ cập nhật.

## Phase 5: SLO/Error Budget
- **Hypothesis**: [Chưa hoàn thiện - Phần việc của thành viên khác] SLO gốc không bao giờ đưa ra cảnh báo (page). Cần một chính sách đa cửa sổ (multi-window policy) để tránh báo động giả gây mệt mỏi (alert fatigue).
- **Agent Proposal**: [TODO] Chờ cập nhật.
- **Test/Evidence**: [TODO] Chờ cập nhật.
- **Decision**: [TODO] Chờ cập nhật.

## Phase 6 & 7: Incident Response
- **Hypothesis**: Cần thực hành quy trình phản ứng với sự cố (incident response) trên một lỗi sụt giảm dữ liệu (volume drop).
- **Agent Proposal**: Chạy kịch bản lỗi `inject_fault.py volume_drop`, phân tích các chỉ số, viết báo cáo sự cố (incident report) trình bày chi tiết cách phát hiện và khắc phục, sau đó chạy `reset_lab.py` để khôi phục trạng thái ban đầu.
- **Test/Evidence**: Baseline đã được khôi phục và toàn bộ tests đã pass.
- **Decision**: Chấp nhận (Accepted).
