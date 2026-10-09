export type OrderStatus = 'pending' | 'packed';

export type Order = {
  id: string;
  customer: string;
  items: string;
  dueAt: string;
  status: OrderStatus;
};

const minutes = (n: number) => new Date(Date.now() + n * 60_000).toISOString();

// Sample orders for the demo. Due times are relative to server start so the
// board always has a realistic mix of late, due-soon, and later pickups.
const orders: Order[] = [
  { id: 'ORD-1042', customer: 'Priya N.', items: '2x Sourdough loaf, 1x Dozen bagels', dueAt: minutes(-55), status: 'pending' },
  { id: 'ORD-1043', customer: 'Marcus T.', items: '1x Birthday cake (chocolate, "Happy 7th Leo")', dueAt: minutes(-20), status: 'pending' },
  { id: 'ORD-1044', customer: 'The Oak Cafe', items: '3x Croissant box (12ct)', dueAt: minutes(-5), status: 'pending' },
  { id: 'ORD-1045', customer: 'Dana R.', items: '1x Gluten-free loaf, 6x Blueberry muffin', dueAt: minutes(15), status: 'pending' },
  { id: 'ORD-1046', customer: 'Sam W.', items: '1x Dozen chocolate chip cookies', dueAt: minutes(40), status: 'pending' },
  { id: 'ORD-1047', customer: 'Elena F.', items: '1x Wedding cake tasting box', dueAt: minutes(90), status: 'pending' },
  { id: 'ORD-1041', customer: 'Jordan K.', items: '1x Rye loaf, 2x Cinnamon rolls', dueAt: minutes(-70), status: 'packed' },
];

export function listOrders(): Order[] {
  return orders;
}

export function markOrderPacked(id: string): Order | undefined {
  const order = orders.find((o) => o.id === id);
  if (!order) return undefined;
  order.status = 'packed';
  return order;
}
