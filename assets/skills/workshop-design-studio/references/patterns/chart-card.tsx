/**
 * Chart card — a chart with a title, a plain-language read, and provenance.
 *
 * This pattern owns the *frame*: the card, the heading, the sentence that says
 * what the chart shows, and the footnote that says where the data came from.
 *
 * It deliberately does NOT choose the chart type. Trend vs comparison vs
 * part-of-whole, the scale, and the semantic colour of the series belong to
 * `databricks-app-design` — on any chart-vocabulary conflict, IBCS wins. Swap
 * `LineChart` below for whatever that skill says this data calls for.
 *
 * AppKit charts are ECharts-based. Configure them with props, never with
 * Recharts-style `<XAxis>` children, and `parameters` is required in query mode
 * even when the query takes none.
 */
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  LineChart,
} from '@databricks/appkit-ui/react';

export function ChartCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">On-time delivery, last 12 weeks</CardTitle>
        {/* Say what the chart shows. A title alone makes the reader do the work. */}
        <CardDescription>
          Steady around 94% since the depot change in week 5.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Query mode: the component fetches, and handles its own loading and
            error states. Do not also call useAnalyticsQuery for this data. */}
        <LineChart
          queryKey="on_time_by_week"
          parameters={{}}
          xKey="week"
          yKey="on_time_rate"
          height={280}
          smooth
          colorPalette="categorical"
          ariaLabel="On-time delivery rate by week for the last twelve weeks"
        />

        {/* Provenance. A number with no source is a number nobody can act on. */}
        <p className="text-xs text-muted-foreground">
          Source: <code className="font-mono">deliveries.weekly_on_time</code> · refreshed hourly
        </p>
      </CardContent>
    </Card>
  );
}
