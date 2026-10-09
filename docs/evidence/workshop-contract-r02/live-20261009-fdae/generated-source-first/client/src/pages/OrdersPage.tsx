import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
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
} from '@databricks/appkit-ui/react';
import { AlertTriangle, CroissantIcon, Package, PackageCheck, Truck, Undo2 } from 'lucide-react';

type BakeryOrder = {
  id: string;
  customer: string;
  items: string;
  fulfillment: 'pickup' | 'delivery';
  dueAt: string;
  packed: boolean;
  packedAt: string | null;
  notes: string | null;
};

type OrdersResponse = { orders: BakeryOrder[]; now: string };

function relativeToNow(iso: string, now: number): string {
  const diffMinutes = Math.round((Date.parse(iso) - now) / 60_000);
  const abs = Math.abs(diffMinutes);
  const unit = abs < 60 ? `${abs} min` : `${Math.round(abs / 60)} hr`;
  return diffMinutes <= 0 ? `${unit} ago` : `in ${unit}`;
}

function isLate(order: BakeryOrder, now: number): boolean {
  return !order.packed && Date.parse(order.dueAt) < now;
}

function OrderStatus({ order, now }: { order: BakeryOrder; now: number }) {
  if (order.packed) {
    return (
      <Badge variant="secondary" className="gap-1.5">
        <PackageCheck className="size-3.5" aria-hidden="true" />
        Packed
      </Badge>
    );
  }
  if (isLate(order, now)) {
    return (
      <Badge variant="destructive" className="gap-1.5">
        <AlertTriangle className="size-3.5" aria-hidden="true" />
        Late — {relativeToNow(order.dueAt, now)}
      </Badge>
    );
  }
  return <Badge variant="outline">Due {relativeToNow(order.dueAt, now)}</Badge>;
}

function PackAction({ order, onToggle }: { order: BakeryOrder; onToggle: (id: string) => void }) {
  if (order.packed) {
    return (
      <Button size="sm" variant="ghost" onClick={() => onToggle(order.id)} className="text-muted-foreground">
        <Undo2 className="size-3.5" aria-hidden="true" />
        Undo
      </Button>
    );
  }
  return (
    <Button size="sm" onClick={() => onToggle(order.id)}>
      <Package className="size-3.5" aria-hidden="true" />
      Mark packed
    </Button>
  );
}

function FulfillmentTag({ fulfillment }: { fulfillment: BakeryOrder['fulfillment'] }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-sm text-muted-foreground">
      <Truck className="size-3.5" aria-hidden="true" />
      {fulfillment === 'pickup' ? 'Pickup' : 'Delivery'}
    </span>
  );
}

function LoadingState() {
  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading today's orders</span>
      {[0, 1, 2, 3].map((i) => (
        <Card key={i}>
          <CardContent className="flex items-center justify-between gap-4 py-4">
            <div className="space-y-2">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-3 w-56" />
            </div>
            <Skeleton className="h-8 w-24" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <Alert variant="destructive">
      <AlertTriangle className="size-4" aria-hidden="true" />
      <AlertTitle>Couldn't load today's orders</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>The order board couldn't reach the server. Nothing's been lost — try again.</p>
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function EmptyBoard() {
  return (
    <Empty className="rounded-xl border border-dashed border-border py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <CroissantIcon className="size-6" aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>No orders on the board</EmptyTitle>
        <EmptyDescription>New pickups and deliveries will show up here as they come in.</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}

export function OrdersPage() {
  const [data, setData] = useState<OrdersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async () => {
    setError(false);
    try {
      const res = await fetch('/api/orders');
      if (!res.ok) throw new Error(`${res.status}`);
      const json: OrdersResponse = await res.json();
      setData(json);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = useCallback(
    async (id: string) => {
      // Optimistic update so the board feels instant at the counter.
      setData((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          orders: prev.orders.map((o) => (o.id === id ? { ...o, packed: !o.packed } : o)),
        };
      });
      try {
        const res = await fetch(`/api/orders/${id}/toggle-packed`, { method: 'POST' });
        if (!res.ok) throw new Error(`${res.status}`);
        await load();
      } catch {
        await load();
      }
    },
    [load],
  );

  const now = data ? Date.parse(data.now) : Date.now();
  const orders = data?.orders ?? [];
  const needsAttention = orders.filter((o) => isLate(o, now));
  const dueSoon = orders.filter((o) => !o.packed && !isLate(o, now));
  const packedToday = orders.filter((o) => o.packed);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-5xl items-center gap-2 px-6 py-4">
          <CroissantIcon className="size-5 text-primary" aria-hidden="true" />
          <span className="text-base font-semibold tracking-tight">Order board</span>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-10 px-6 py-10">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Today's orders</h1>
          <p className="max-w-prose text-sm text-muted-foreground">
            Late, unpacked orders surface first so you always know what to grab next.
          </p>
        </div>

        {loading ? (
          <LoadingState />
        ) : error ? (
          <ErrorState onRetry={load} />
        ) : orders.length === 0 ? (
          <EmptyBoard />
        ) : (
          <>
            {/* The focal point: how many orders need attention, right now. */}
            <div className="grid gap-4 sm:grid-cols-3">
              <Card className={needsAttention.length > 0 ? 'border-destructive/40' : undefined}>
                <CardHeader className="pb-2">
                  <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Needs attention
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p
                    className={
                      needsAttention.length > 0
                        ? 'text-4xl font-semibold tabular-nums text-destructive'
                        : 'text-4xl font-semibold tabular-nums text-foreground'
                    }
                  >
                    {needsAttention.length}
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">late and not packed</p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Due soon
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-4xl font-semibold tabular-nums">{dueSoon.length}</p>
                  <p className="mt-1 text-sm text-muted-foreground">not packed yet</p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Packed
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-4xl font-semibold tabular-nums">{packedToday.length}</p>
                  <p className="mt-1 text-sm text-muted-foreground">ready to go</p>
                </CardContent>
              </Card>
            </div>

            {/* Table on wider screens — numbers and actions line up in columns. */}
            <Card className="hidden sm:block">
              <CardContent className="pt-6">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Order</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Customer</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Items</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Fulfillment</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Status</TableHead>
                      <TableHead className="text-right text-xs font-medium uppercase tracking-wide">Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {orders.map((order) => (
                      <TableRow key={order.id} className={isLate(order, now) ? 'bg-destructive/5' : undefined}>
                        <TableCell className="font-mono text-xs text-muted-foreground">{order.id}</TableCell>
                        <TableCell className="font-medium">{order.customer}</TableCell>
                        <TableCell className="max-w-xs text-sm text-muted-foreground">
                          <span>{order.items}</span>
                          {order.notes && <span className="mt-0.5 block text-xs italic">{order.notes}</span>}
                        </TableCell>
                        <TableCell>
                          <FulfillmentTag fulfillment={order.fulfillment} />
                        </TableCell>
                        <TableCell>
                          <OrderStatus order={order} now={now} />
                        </TableCell>
                        <TableCell className="text-right">
                          <PackAction order={order} onToggle={toggle} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>

            {/* Cards on narrow screens — status and action stay reachable with no horizontal scroll. */}
            <div className="space-y-3 sm:hidden">
              {orders.map((order) => (
                <Card key={order.id} className={isLate(order, now) ? 'border-destructive/40' : undefined}>
                  <CardContent className="space-y-3 py-4">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="font-medium">{order.customer}</p>
                        <p className="font-mono text-xs text-muted-foreground">{order.id}</p>
                      </div>
                      <OrderStatus order={order} now={now} />
                    </div>
                    <p className="text-sm text-muted-foreground">{order.items}</p>
                    {order.notes && <p className="text-xs italic text-muted-foreground">{order.notes}</p>}
                    <div className="flex items-center justify-between pt-1">
                      <FulfillmentTag fulfillment={order.fulfillment} />
                      <PackAction order={order} onToggle={toggle} />
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
