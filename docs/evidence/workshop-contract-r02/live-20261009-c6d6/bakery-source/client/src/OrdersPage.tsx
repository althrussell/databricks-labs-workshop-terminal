import { useEffect, useState, useCallback } from 'react';
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
} from '@databricks/appkit-ui/react';
import { AlertTriangle, Clock, Croissant, PackageCheck } from 'lucide-react';

type OrderStatus = 'pending' | 'packed';

type Order = {
  id: string;
  customer: string;
  items: string;
  dueAt: string;
  status: OrderStatus;
};

function minutesUntil(dueAt: string, now: number) {
  return Math.round((new Date(dueAt).getTime() - now) / 60_000);
}

function dueLabel(dueAt: string, now: number) {
  const mins = minutesUntil(dueAt, now);
  const time = new Date(dueAt).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (mins < 0) return `${time} · ${Math.abs(mins)} min late`;
  if (mins === 0) return `${time} · due now`;
  return `${time} · in ${mins} min`;
}

function rank(order: Order, now: number) {
  if (order.status === 'packed') return 3;
  const mins = minutesUntil(order.dueAt, now);
  if (mins < 0) return 0; // late, unpacked — needs attention first
  if (mins <= 60) return 1; // due soon
  return 2;
}

export function OrdersPage() {
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [error, setError] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [packingId, setPackingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(false);
    try {
      const res = await fetch('/api/orders');
      if (!res.ok) throw new Error('request failed');
      const data = await res.json();
      setOrders(data.orders);
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Keep "late" / "due soon" accurate without needing a page reload.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const markPacked = async (id: string) => {
    setPackingId(id);
    setOrders((prev) => prev?.map((o) => (o.id === id ? { ...o, status: 'packed' } : o)) ?? prev);
    try {
      const res = await fetch(`/api/orders/${id}/pack`, { method: 'PATCH' });
      if (!res.ok) throw new Error('request failed');
    } catch {
      // Roll back the optimistic update if the server didn't confirm it.
      setOrders((prev) => prev?.map((o) => (o.id === id ? { ...o, status: 'pending' } : o)) ?? prev);
    } finally {
      setPackingId(null);
    }
  };

  const needsAttention = orders?.filter((o) => o.status === 'pending' && minutesUntil(o.dueAt, now) < 0) ?? [];
  const dueSoon = orders?.filter((o) => {
    const mins = minutesUntil(o.dueAt, now);
    return o.status === 'pending' && mins >= 0 && mins <= 60;
  }) ?? [];
  const packedToday = orders?.filter((o) => o.status === 'packed') ?? [];

  const sorted = orders ? [...orders].sort((a, b) => {
    const r = rank(a, now) - rank(b, now);
    if (r !== 0) return r;
    return new Date(a.dueAt).getTime() - new Date(b.dueAt).getTime();
  }) : [];

  return (
    <div className="mx-auto max-w-5xl space-y-10 px-4 py-8 md:px-6 md:py-10">
      <div className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Order board</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Late, unpacked orders surface first so your team always knows what to pack next.
        </p>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" aria-hidden="true" />
          <AlertTitle>We couldn&apos;t load today&apos;s orders</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>This is usually temporary — your orders are safe.</p>
            <Button size="sm" variant="outline" onClick={load}>
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {!error && orders === null && (
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
      )}

      {!error && orders !== null && (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card className="border-destructive/30 bg-destructive/5">
              <CardHeader className="pb-2">
                <CardDescription className="text-xs font-medium uppercase tracking-wide">
                  Needs attention
                </CardDescription>
                <CardTitle className="text-4xl font-semibold tabular-nums tracking-tight text-destructive">
                  {needsAttention.length}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">late and not yet packed</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="text-xs font-medium uppercase tracking-wide">Due soon</CardDescription>
                <CardTitle className="text-4xl font-semibold tabular-nums tracking-tight">
                  {dueSoon.length}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">pickup within the hour</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription className="text-xs font-medium uppercase tracking-wide">Packed</CardDescription>
                <CardTitle className="text-4xl font-semibold tabular-nums tracking-tight">
                  {packedToday.length}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">ready for pickup</p>
              </CardContent>
            </Card>
          </div>

          {sorted.length === 0 ? (
            <Empty className="rounded-xl border border-dashed border-border py-16">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <Croissant className="size-6" aria-hidden="true" />
                </EmptyMedia>
                <EmptyTitle>No orders yet</EmptyTitle>
                <EmptyDescription>New orders will show up here the moment they come in.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Card className="overflow-hidden">
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Order</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Items</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Due</TableHead>
                      <TableHead className="text-xs font-medium uppercase tracking-wide">Status</TableHead>
                      <TableHead className="text-right text-xs font-medium uppercase tracking-wide">Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sorted.map((order) => {
                      const mins = minutesUntil(order.dueAt, now);
                      const isLate = order.status === 'pending' && mins < 0;
                      const isSoon = order.status === 'pending' && mins >= 0 && mins <= 60;
                      return (
                        <TableRow
                          key={order.id}
                          className="transition-colors duration-300 motion-reduce:transition-none"
                        >
                          <TableCell>
                            <div className="font-medium">{order.customer}</div>
                            <div className="font-mono text-xs text-muted-foreground">{order.id}</div>
                          </TableCell>
                          <TableCell className="max-w-xs text-muted-foreground">{order.items}</TableCell>
                          <TableCell className="whitespace-nowrap">
                            <span
                              className={
                                isLate
                                  ? 'flex items-center gap-1.5 font-medium text-destructive'
                                  : 'flex items-center gap-1.5 text-muted-foreground'
                              }
                            >
                              <Clock className="size-3.5" aria-hidden="true" />
                              {dueLabel(order.dueAt, now)}
                            </span>
                          </TableCell>
                          <TableCell>
                            {order.status === 'packed' ? (
                              <Badge variant="secondary">
                                <PackageCheck className="mr-1 size-3" aria-hidden="true" />
                                Packed
                              </Badge>
                            ) : isLate ? (
                              <Badge variant="destructive">Late</Badge>
                            ) : isSoon ? (
                              <Badge variant="outline" className="border-warning text-warning">
                                Due soon
                              </Badge>
                            ) : (
                              <Badge variant="outline">Scheduled</Badge>
                            )}
                          </TableCell>
                          <TableCell className="text-right">
                            {order.status === 'pending' && (
                              <Button
                                size="sm"
                                variant={isLate ? 'default' : 'outline'}
                                disabled={packingId === order.id}
                                onClick={() => markPacked(order.id)}
                              >
                                Mark packed
                              </Button>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
