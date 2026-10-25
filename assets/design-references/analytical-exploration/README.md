# Analytical exploration

**Job:** understand how a measure moves over time or across groups, and trust
what you are looking at.

- Every number carries its unit, period and denominator ("per 1,000 customers",
  "Jul–Sep 2026"). A rate never appears without what it is a rate of.
- Filters change the actual result; the chart and the table always show the same
  filtered rows, and the summary sentence is computed from them.
- Uncertainty is shown, not smoothed away: small groups are flagged, and a
  screening proxy is labelled as a proxy, never as an engineering determination.
- The summary leaves the analysis visible: it sits above the table, it does not
  replace it.
- KPIs show their source ("from `outages_daily`, refreshed 06:00"); none is
  invented.
- Narrow layout: the table becomes label/value rows; the filter stays at the top.

States: `loading`, `empty` (no rows for this filter, with how to widen it),
`error`, `long-label` (long group names wrap in the table, truncate with a title
in the chart axis).

Checks: the chart has a text alternative (the table); colour is never the only
encoding; focus visible; contrast AA.
