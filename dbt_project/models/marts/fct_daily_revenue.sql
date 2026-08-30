-- Daily completed-order revenue for the CEO dashboard.
--
-- The customer dimension is an SCD: a customer can have several rows, and a bad
-- load can leave more than one of them flagged active. Joining it directly fans
-- out the order rows and inflates `daily_revenue` with no SQL error at all - see
-- the `duplicate_active_customer_does_not_inflate_revenue` unit test, which fails
-- against the naive join. We therefore collapse the dimension to one current row
-- per customer before joining.

with completed_orders as (
    select *
    from {{ ref('stg_orders') }}
    where status = 'completed'
),
ranked_customers as (
    select
        *,
        row_number() over (
            partition by customer_id
            order by
                case when valid_to is null then 1 else 0 end desc,
                valid_from desc
        ) as scd_rank
    from {{ ref('stg_customers') }}
    where is_active = true
),
active_customers as (
    select *
    from ranked_customers
    where scd_rank = 1
)
select
    o.order_date,
    count(*) as completed_order_rows,
    sum(o.amount_usd) as daily_revenue
from completed_orders o
left join active_customers c
    on o.customer_id = c.customer_id
group by 1
order by 1
