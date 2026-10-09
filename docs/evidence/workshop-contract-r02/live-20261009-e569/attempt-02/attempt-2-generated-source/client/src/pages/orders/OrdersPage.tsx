import { useMemo } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  useAnalyticsQuery,
} from '@databricks/appkit-ui/react';
import { AlertTriangle, PackageCheck, PartyPopper } from 'lucide-react';
import { daysOverdue, formatCurrency, formatDueDate } from '../../lib/formatters';

const EMPTY_PARAMS = {};

function LoadingState() {
  return (
    <div className="space-y-10">
      <div className="grid gap-4 sm:grid-cols-3" aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading orders</span>
        {[0, 1, 2].map((i) => (
          <Card key={i}>
            <CardHeader className="pb-2">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="mt-2 h-9 w-16" />
            </CardHeader>
          </Card>
        ))}
      </div>
      <Card>
        <CardContent className="pt-6 space-y-3">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <Alert variant="destructive">
      <AlertTriangle className="size-4" aria-hidden="true" />
      <AlertTitle>Couldn't load your orders</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>{message}</p>
        <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
          Try again
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function EmptyOrdersState() {
  return (
    <Empty className="rounded-xl border border-dashed border-border py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <PartyPopper className="size-6" aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>No orders yet</EmptyTitle>
        <EmptyDescription>
          New orders will show up here the moment they come in.
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

function AllCaughtUpState() {
  return (
    <Empty className="rounded-xl border border-dashed border-border py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <PackageCheck className="size-6" aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>Nothing needs attention</EmptyTitle>
        <EmptyDescription>
          Every order is either packed or still on time. Nice work.
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

export function OrdersPage() {
  const { data, loading, error } = useAnalyticsQuery('bakery_orders', EMPTY_PARAMS);

  const orders = data ?? [];

  const stats = useMemo(() => {
    const needsAttention = orders.filter((o) => o.needs_attention).length;
    const openOrders = orders.filter((o) => !o.packed).length;
    const packedToday = orders.filter((o) => o.packed).length;
    return { needsAttention, openOrders, packedToday };
  }, [orders]);

  const attentionOrders = orders.filter((o) => o.needs_attention);
  const otherOrders = orders.filter((o) => !o.needs_attention);

  return (
    <div className="mx-auto max-w-5xl space-y-10 px-4 py-10 md:px-8">
      <div className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Order board</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          {loading
            ? 'Checking your orders…'
            : stats.needsAttention > 0
              ? `${stats.needsAttention} order${stats.needsAttention === 1 ? '' : 's'} ${stats.needsAttention === 1 ? 'is' : 'are'} late and still unpacked — pack these first.`
              : 'Nothing is overdue right now.'}
        </p>
      </div>

      {loading && <LoadingState />}
      {!loading && error && <ErrorState message={String(error)} />}

      {!loading && !error && orders.length === 0 && <EmptyOrdersState />}

      {!loading && !error && orders.length > 0 && (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card className={stats.needsAttention > 0 ? 'border-destructive/40' : undefined}>
              <CardHeader className="pb-2">
                <CardDescription className="text-xs font-medium uppercase tracking-wide">
                  Needs attention
                </CardDescription>
                <CardTitle
                  className={`text-4xl font-semibold tabular-nums tracking-tight ${
                    stats.needsAttention > 0 ? 'text-destructive' : ''
                  }`}
                >
                  {stats.needsAttention}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">late &amp; unpacked</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="text-xs font-medium uppercase tracking-wide">
                  Open orders
                </CardDescription>
                <CardTitle className="text-4xl font-semibold tabular-nums tracking-tight">
                  {stats.openOrders}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">still to pack</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="text-xs font-medium uppercase tracking-wide">
                  Packed
                </CardDescription>
                <CardTitle className="text-4xl font-semibold tabular-nums tracking-tight">
                  {stats.packedToday}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">ready to go</p>
              </CardContent>
            </Card>
          </div>

          <div className="space-y-4">
            <h2 className="text-xl font-semibold tracking-tight text-foreground">
              Needs attention
            </h2>
            {attentionOrders.length === 0 ? (
              <AllCaughtUpState />
            ) : (
              <Card className="border-destructive/30">
                <CardContent className="pt-6">
                  <OrdersTable rows={attentionOrders} />
                </CardContent>
              </Card>
            )}
          </div>

          {otherOrders.length > 0 && (
            <div className="space-y-4">
              <h2 className="text-xl font-semibold tracking-tight text-foreground">
                Everything else
              </h2>
              <Card>
                <CardContent className="pt-6">
                  <OrdersTable rows={otherOrders} />
                </CardContent>
              </Card>
            </div>
          )}
        </>
      )}
    </div>
  );
}

type OrderRow = {
  order_id: string;
  customer_name: string;
  items: string;
  quantity: number;
  order_total: number;
  due_at: string;
  packed: boolean;
  needs_attention: boolean;
};

function OrdersTable({ rows }: { rows: OrderRow[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Order</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Customer</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Items</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Due</TableHead>
          <TableHead className="text-right text-xs font-medium uppercase tracking-wide">
            Total
          </TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.order_id}>
            <TableCell className="font-mono text-xs text-muted-foreground">
              {row.order_id}
            </TableCell>
            <TableCell className="font-medium">{row.customer_name}</TableCell>
            <TableCell className="text-muted-foreground">
              {row.items} × {row.quantity}
            </TableCell>
            <TableCell>
              {formatDueDate(row.due_at)}
              {row.needs_attention && (
                <span className="ml-1.5 text-xs font-medium text-destructive">
                  · {daysOverdue(row.due_at)}d late
                </span>
              )}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {formatCurrency(row.order_total)}
            </TableCell>
            <TableCell>
              {row.needs_attention ? (
                <Badge variant="destructive">Needs attention</Badge>
              ) : row.packed ? (
                <Badge variant="secondary">Packed</Badge>
              ) : (
                <Badge variant="outline">On track</Badge>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
