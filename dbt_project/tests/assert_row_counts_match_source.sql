-- Business rule: every completed order must be counted exactly once.
-- `assert_no_revenue_inflation` catches the amount side; this catches the row
-- side, which is what a fan-out join breaks first.
with expected as (
    select order_date, count(*) as expected_rows
    from {{ ref('stg_orders') }}
    where status = 'completed'
    group by 1
)
select
    f.order_date,
    f.completed_order_rows,
    e.expected_rows
from {{ ref('fct_daily_revenue') }} f
join expected e on f.order_date = e.order_date
where f.completed_order_rows != e.expected_rows
