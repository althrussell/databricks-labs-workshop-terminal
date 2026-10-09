SELECT
  order_id,
  customer_name,
  items,
  quantity,
  order_total,
  due_at,
  packed,
  (packed = false AND due_at < current_timestamp()) AS needs_attention
FROM wt_eval_r02_1009_e569.generated_apps.bakery_orders
ORDER BY needs_attention DESC, due_at ASC
