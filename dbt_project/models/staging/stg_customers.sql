select
    cast(customer_id as varchar) as customer_id,
    cast(country as varchar) as country,
    cast(tier as varchar) as tier,
    cast(is_active as boolean) as is_active,
    try_cast(valid_from as timestamp) as valid_from,
    -- The current SCD row has an empty valid_to. `try_cast` keeps that NULL
    -- whether the seed loader typed the column as VARCHAR or TIMESTAMP;
    -- `nullif(valid_to, '')` crashes once DuckDB infers TIMESTAMP.
    try_cast(valid_to as timestamp) as valid_to
from {{ ref('customers') }}
