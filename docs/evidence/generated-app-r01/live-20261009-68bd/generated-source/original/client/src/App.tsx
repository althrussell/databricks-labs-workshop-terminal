import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
  Input,
  Label,
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Textarea,
} from '@databricks/appkit-ui/react';
import { AlertTriangle, Check, CroissantIcon, Plus } from 'lucide-react';

type Order = {
  id: number;
  customer_name: string;
  item: string;
  due_date: string;
  notes: string | null;
  packed: boolean;
  created_at: string;
};

type NewOrder = { customerName: string; item: string; dueDate: string; notes: string };

function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

function orderUrgency(order: Order): 'late' | 'today' | 'upcoming' | 'packed' {
  if (order.packed) return 'packed';
  if (order.due_date < todayStr()) return 'late';
  if (order.due_date === todayStr()) return 'today';
  return 'upcoming';
}

const URGENCY_LABEL: Record<ReturnType<typeof orderUrgency>, string> = {
  late: 'Late',
  today: 'Due today',
  upcoming: 'Upcoming',
  packed: 'Packed',
};

const URGENCY_VARIANT: Record<ReturnType<typeof orderUrgency>, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  late: 'destructive',
  today: 'default',
  upcoming: 'outline',
  packed: 'secondary',
};

function formatDue(dateStr: string) {
  const date = new Date(`${dateStr}T00:00:00`);
  return date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

export default function App() {
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);

  const load = async () => {
    setError(null);
    try {
      const res = await fetch('/api/lakebase/orders');
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = (await res.json()) as Order[];
      setOrders(data);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const lateCount = useMemo(
    () => (orders ?? []).filter((o) => orderUrgency(o) === 'late').length,
    [orders],
  );

  const togglePacked = async (order: Order) => {
    setOrders((prev) =>
      prev ? prev.map((o) => (o.id === order.id ? { ...o, packed: !o.packed } : o)) : prev,
    );
    try {
      const res = await fetch(`/api/lakebase/orders/${order.id}/packed`, { method: 'PATCH' });
      if (!res.ok) throw new Error('Failed to update order');
      const updated = (await res.json()) as Order;
      setOrders((prev) => (prev ? prev.map((o) => (o.id === order.id ? updated : o)) : prev));
    } catch {
      // Revert the optimistic flip; the next load() or retry will reconcile.
      setOrders((prev) =>
        prev ? prev.map((o) => (o.id === order.id ? { ...o, packed: order.packed } : o)) : prev,
      );
    }
  };

  const createOrder = async (values: NewOrder) => {
    const res = await fetch('/api/lakebase/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    });
    if (!res.ok) throw new Error('Failed to add order');
    const created = (await res.json()) as Order;
    setOrders((prev) => (prev ? sortOrders([...prev, created]) : [created]));
  };

  const sorted = orders ? sortOrders(orders) : null;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-6 py-4">
          <CroissantIcon className="size-6 text-primary" aria-hidden="true" />
          <span className="text-base font-semibold tracking-tight">
            Order Board<span className="text-primary">.</span>
          </span>
          <div className="ml-auto">
            <AddOrderDialog open={dialogOpen} onOpenChange={setDialogOpen} onCreate={createOrder} />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-8 px-6 py-10">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Today's orders</h1>
          <p className="max-w-prose text-sm text-muted-foreground">
            Late and unpacked orders surface first, so you always know what to grab next.
          </p>
        </div>

        <HeroStat loading={orders === null} count={lateCount} />

        {error && (
          <Alert variant="destructive">
            <AlertTriangle className="size-4" aria-hidden="true" />
            <AlertTitle>We couldn't load your orders</AlertTitle>
            <AlertDescription className="space-y-3">
              <p>The connection timed out. Your data is safe — this is usually temporary.</p>
              <Button size="sm" variant="outline" onClick={load}>
                Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {!error && orders === null && <TableSkeleton />}

        {!error && sorted !== null && sorted.length === 0 && (
          <Empty className="rounded-xl border border-dashed border-border py-16">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <CroissantIcon className="size-6" aria-hidden="true" />
              </EmptyMedia>
              <EmptyTitle>No orders yet</EmptyTitle>
              <EmptyDescription>
                Add your first order and it'll show up here, sorted by what needs attention.
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Button onClick={() => setDialogOpen(true)}>
                <Plus className="size-4" aria-hidden="true" />
                Add an order
              </Button>
            </EmptyContent>
          </Empty>
        )}

        {!error && sorted !== null && sorted.length > 0 && (
          <OrdersTable orders={sorted} onTogglePacked={togglePacked} />
        )}
      </main>
    </div>
  );
}

function sortOrders(orders: Order[]): Order[] {
  const rank: Record<ReturnType<typeof orderUrgency>, number> = { late: 0, today: 1, upcoming: 2, packed: 3 };
  return [...orders].sort((a, b) => {
    const r = rank[orderUrgency(a)] - rank[orderUrgency(b)];
    if (r !== 0) return r;
    return a.due_date.localeCompare(b.due_date);
  });
}

function HeroStat({ loading, count }: { loading: boolean; count: number }) {
  if (loading) {
    return (
      <div className="rounded-xl border border-border bg-card p-6">
        <Skeleton className="h-3 w-40" />
        <Skeleton className="mt-3 h-10 w-16" />
      </div>
    );
  }

  const allClear = count === 0;
  return (
    <div
      className={
        allClear
          ? 'rounded-xl border border-border bg-card p-6'
          : 'rounded-xl border border-destructive/30 bg-destructive/5 p-6'
      }
    >
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Needs attention
      </p>
      <div className="mt-1 flex items-baseline gap-3">
        <span
          className={
            allClear
              ? 'text-5xl font-semibold tabular-nums tracking-tight text-foreground'
              : 'text-5xl font-semibold tabular-nums tracking-tight text-destructive'
          }
        >
          {count}
        </span>
        <span className="text-sm text-muted-foreground">
          {allClear ? "order late and unpacked — you're all caught up" : count === 1 ? 'order late and unpacked' : 'orders late and unpacked'}
        </span>
      </div>
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading orders</span>
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-14 w-full rounded-lg" />
      ))}
    </div>
  );
}

function OrdersTable({ orders, onTogglePacked }: { orders: Order[]; onTogglePacked: (order: Order) => void }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Customer</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Order</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Due</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Status</TableHead>
          <TableHead className="text-right text-xs font-medium uppercase tracking-wide">Packed</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {orders.map((order) => {
          const urgency = orderUrgency(order);
          return (
            <TableRow key={order.id}>
              <TableCell className="font-medium">{order.customer_name}</TableCell>
              <TableCell className="text-muted-foreground">
                {order.item}
                {order.notes && (
                  <span className="block text-xs text-muted-foreground/80">{order.notes}</span>
                )}
              </TableCell>
              <TableCell className="whitespace-nowrap">{formatDue(order.due_date)}</TableCell>
              <TableCell>
                <Badge variant={URGENCY_VARIANT[urgency]}>{URGENCY_LABEL[urgency]}</Badge>
              </TableCell>
              <TableCell className="text-right">
                <Button
                  size="sm"
                  variant={order.packed ? 'outline' : 'default'}
                  onClick={() => onTogglePacked(order)}
                >
                  {order.packed ? (
                    'Packed'
                  ) : (
                    <>
                      <Check className="size-4" aria-hidden="true" />
                      Mark packed
                    </>
                  )}
                </Button>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function AddOrderDialog({
  open,
  onOpenChange,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreate: (values: NewOrder) => Promise<void>;
}) {
  const empty: NewOrder = { customerName: '', item: '', dueDate: todayStr(), notes: '' };
  const [values, setValues] = useState<NewOrder>(empty);
  const [errors, setErrors] = useState<Partial<Record<keyof NewOrder, string>>>({});
  const [submitting, setSubmitting] = useState(false);

  const validate = (v: NewOrder) => {
    const errs: Partial<Record<keyof NewOrder, string>> = {};
    if (!v.customerName.trim()) errs.customerName = 'Who is this order for?';
    if (!v.item.trim()) errs.item = "What are they ordering?";
    if (!v.dueDate) errs.dueDate = 'When is it due?';
    return errs;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const found = validate(values);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    setSubmitting(true);
    try {
      await onCreate(values);
      setValues(empty);
      onOpenChange(false);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) {
          setValues(empty);
          setErrors({});
        }
      }}
    >
      <DialogTrigger asChild>
        <Button>
          <Plus className="size-4" aria-hidden="true" />
          New order
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[425px]">
        <form onSubmit={handleSubmit} noValidate>
          <DialogHeader>
            <DialogTitle>Add an order</DialogTitle>
            <DialogDescription>It'll show up on the board, sorted by due date.</DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="customerName">Customer</Label>
              <Input
                id="customerName"
                placeholder="e.g. Priya Shah"
                value={values.customerName}
                onChange={(e) => setValues((v) => ({ ...v, customerName: e.target.value }))}
                aria-invalid={Boolean(errors.customerName)}
              />
              {errors.customerName && (
                <p className="text-sm text-destructive">{errors.customerName}</p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="item">Order</Label>
              <Input
                id="item"
                placeholder="e.g. 2 dozen chocolate chip cookies"
                value={values.item}
                onChange={(e) => setValues((v) => ({ ...v, item: e.target.value }))}
                aria-invalid={Boolean(errors.item)}
              />
              {errors.item && <p className="text-sm text-destructive">{errors.item}</p>}
            </div>

            <div className="space-y-2">
              <Label htmlFor="dueDate">Due date</Label>
              <Input
                id="dueDate"
                type="date"
                value={values.dueDate}
                onChange={(e) => setValues((v) => ({ ...v, dueDate: e.target.value }))}
                aria-invalid={Boolean(errors.dueDate)}
              />
              {errors.dueDate && <p className="text-sm text-destructive">{errors.dueDate}</p>}
            </div>

            <div className="space-y-2">
              <Label htmlFor="notes">Notes (optional)</Label>
              <Textarea
                id="notes"
                placeholder="e.g. nut allergy, pickup after 2pm"
                value={values.notes}
                onChange={(e) => setValues((v) => ({ ...v, notes: e.target.value }))}
              />
            </div>
          </div>

          <DialogFooter>
            <Button type="submit" disabled={submitting}>
              {submitting ? 'Adding…' : 'Add order'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
