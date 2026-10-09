import { createApp, server } from '@databricks/appkit';

export type BakeryOrder = {
  id: string;
  customer: string;
  items: string;
  fulfillment: 'pickup' | 'delivery';
  dueAt: string;
  packed: boolean;
  packedAt: string | null;
  notes: string | null;
};

// Due times are relative to server start so "late" and "due soon" stay true
// no matter when this demo is actually run.
const START = Date.now();
const at = (offsetMinutes: number) => new Date(START + offsetMinutes * 60_000).toISOString();

const orders: BakeryOrder[] = [
  {
    id: 'ORD-104',
    customer: 'Priya N.',
    items: '2x Sourdough, 1x Rye loaf',
    fulfillment: 'pickup',
    dueAt: at(-55),
    packed: false,
    packedAt: null,
    notes: null,
  },
  {
    id: 'ORD-108',
    customer: 'The Green Table Cafe',
    items: '24x Croissant, 12x Pain au chocolat',
    fulfillment: 'delivery',
    dueAt: at(-20),
    packed: false,
    packedAt: null,
    notes: 'Wholesale account — leave at the back door',
  },
  {
    id: 'ORD-111',
    customer: 'Marcus O.',
    items: '1x Chocolate birthday cake',
    fulfillment: 'pickup',
    dueAt: at(-8),
    packed: false,
    packedAt: null,
    notes: 'Custom cake — confirm "Happy 40th Dana" spelling before boxing',
  },
  {
    id: 'ORD-115',
    customer: 'Aisha K.',
    items: '6x Blueberry muffin',
    fulfillment: 'pickup',
    dueAt: at(15),
    packed: false,
    packedAt: null,
    notes: null,
  },
  {
    id: 'ORD-117',
    customer: 'Tom R.',
    items: '1x Baguette, 4x Pain au chocolat',
    fulfillment: 'pickup',
    dueAt: at(40),
    packed: false,
    packedAt: null,
    notes: null,
  },
  {
    id: 'ORD-119',
    customer: 'Riverside Deli',
    items: '18x Dinner roll tray',
    fulfillment: 'delivery',
    dueAt: at(90),
    packed: false,
    packedAt: null,
    notes: null,
  },
  {
    id: 'ORD-101',
    customer: 'Elena V.',
    items: '2x Sourdough',
    fulfillment: 'pickup',
    dueAt: at(-120),
    packed: true,
    packedAt: at(-100),
    notes: null,
  },
  {
    id: 'ORD-103',
    customer: 'James P.',
    items: '12x Bagel, cream cheese tub',
    fulfillment: 'pickup',
    dueAt: at(-90),
    packed: true,
    packedAt: at(-80),
    notes: null,
  },
];

function attentionRank(order: BakeryOrder): 0 | 1 | 2 {
  if (order.packed) return 2;
  return Date.now() > Date.parse(order.dueAt) ? 0 : 1;
}

function sortedOrders(): BakeryOrder[] {
  return [...orders].sort((a, b) => {
    const rankDiff = attentionRank(a) - attentionRank(b);
    if (rankDiff !== 0) return rankDiff;
    if (attentionRank(a) === 2) {
      return Date.parse(b.packedAt ?? b.dueAt) - Date.parse(a.packedAt ?? a.dueAt);
    }
    return Date.parse(a.dueAt) - Date.parse(b.dueAt);
  });
}

createApp({
  plugins: [server()],
  onPluginsReady(appkit) {
    appkit.server.extend((app) => {
      app.get('/api/orders', (_req, res) => {
        res.json({ orders: sortedOrders(), now: new Date().toISOString() });
      });

      app.post('/api/orders/:id/toggle-packed', (req, res) => {
        const order = orders.find((o) => o.id === req.params.id);
        if (!order) {
          res.status(404).json({ error: 'Order not found' });
          return;
        }
        order.packed = !order.packed;
        order.packedAt = order.packed ? new Date().toISOString() : null;
        res.json({ order, now: new Date().toISOString() });
      });
    });
  },
}).catch(console.error);
