/**
 * KPI row — a set of headline numbers.
 *
 * AppKit ships no prebuilt KPI card, so this composes one from Card primitives.
 * This is a *data surface*: `databricks-app-design` owns the notation rules
 * (units, precision, provenance, what a delta is allowed to imply) and wins on
 * any conflict. This pattern owns how it is composed and spaced.
 *
 * Why it looks designed:
 *  - the value is the largest thing in the card (text-4xl) and uses tabular
 *    figures, so a live number does not jitter as digits change;
 *  - the label is quiet and above the value, so the eye lands on the number;
 *  - direction is carried by an icon *and* a word, never colour alone;
 *  - "up" is not assumed to be good — each metric states its own polarity.
 */
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@databricks/appkit-ui/react';
import { TrendingDown, TrendingUp } from 'lucide-react';

type Kpi = {
  label: string;
  value: string;
  /** Change vs the comparison period, already formatted. */
  delta: string;
  direction: 'up' | 'down';
  /** Whether this metric moving up is a good thing. */
  upIsGood: boolean;
  /** What the number is measured against — never ship a metric without it. */
  context: string;
};

const KPIS: Kpi[] = [
  { label: 'On-time rate', value: '94.2%', delta: '+1.8 pts', direction: 'up', upIsGood: true, context: 'vs last week' },
  { label: 'Active vehicles', value: '128', delta: '+6', direction: 'up', upIsGood: true, context: 'vs last week' },
  { label: 'Avg delay', value: '11 min', delta: '+3 min', direction: 'up', upIsGood: false, context: 'vs last week' },
  { label: 'Open incidents', value: '3', delta: '-2', direction: 'down', upIsGood: false, context: 'vs last week' },
];

export function KpiRow() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {KPIS.map((kpi) => {
        const isGood = kpi.direction === 'up' ? kpi.upIsGood : !kpi.upIsGood;
        const Icon = kpi.direction === 'up' ? TrendingUp : TrendingDown;
        return (
          <Card key={kpi.label}>
            <CardHeader className="pb-2">
              <CardDescription className="text-xs font-medium uppercase tracking-wide">
                {kpi.label}
              </CardDescription>
              <CardTitle className="text-4xl font-semibold tabular-nums tracking-tight">
                {kpi.value}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="flex items-center gap-1.5 text-sm">
                <Icon
                  className={isGood ? 'size-4 text-success' : 'size-4 text-destructive'}
                  aria-hidden="true"
                />
                <span
                  className={
                    isGood
                      ? 'font-medium tabular-nums text-success'
                      : 'font-medium tabular-nums text-destructive'
                  }
                >
                  {kpi.delta}
                </span>
                {/* The word is what makes this readable without colour vision. */}
                <span className="text-muted-foreground">
                  {kpi.direction === 'up' ? 'up' : 'down'} {kpi.context}
                </span>
              </p>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
