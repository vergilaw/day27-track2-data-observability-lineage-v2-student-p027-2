-- Singular test to ensure revenue is not inflated by customer duplication
-- This checks if the daily revenue in fct_daily_revenue exceeds the
-- sum of amounts for completed orders in stg_orders for each day.
with base_revenue as (
    select
        order_date,
        sum(amount_usd) as expected_revenue
    from {{ ref('stg_orders') }}
    where status = 'completed'
    group by 1
),
actual_revenue as (
    select
        order_date,
        daily_revenue
    from {{ ref('fct_daily_revenue') }}
)
select *
from actual_revenue a
join base_revenue b on a.order_date = b.order_date
where a.daily_revenue > b.expected_revenue
