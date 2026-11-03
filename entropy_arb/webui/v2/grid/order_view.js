/* Display intent purpose separately from native whole-position mechanics. */
export function presentOrder(row) {
  const cause = row.cause_key || "";
  const exit = cause.startsWith("exit-flat:");
  let purpose;
  if (exit) purpose = cause.includes(":native-residual:") ? "残仓退出" : "结束平仓";
  else if (cause.startsWith("initial-position:")) purpose = "初始建仓";
  else if (row.whole_position) purpose = "止损保护";
  else if (cause.startsWith("refill:")) purpose = row.reduce_only ? "网格补单 · 减仓" : "网格补单";
  else purpose = row.reduce_only ? "网格减仓" : "网格开仓";
  return {
    purpose,
    price: row.whole_position ? `${row.trigger_price} · ${exit ? "退出触发" : "止损触发"}` : row.price,
    quantity: row.whole_position ? "整个仓位" : row.qty_base,
    waitingTrigger: !!row.whole_position && ["OPEN", "ACCEPTED"].includes(row.state),
  };
}
