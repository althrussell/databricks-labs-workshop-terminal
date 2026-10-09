import { createApp, server } from '@databricks/appkit';
import { listOrders, markOrderPacked } from './orders.js';

createApp({
  plugins: [server()],
  onPluginsReady(appkit) {
    appkit.server.extend((app) => {
      app.get('/api/orders', (_req, res) => {
        res.json({ orders: listOrders() });
      });

      app.patch('/api/orders/:id/pack', (req, res) => {
        const order = markOrderPacked(req.params.id);
        if (!order) {
          res.status(404).json({ error: 'Order not found' });
          return;
        }
        res.json({ order });
      });
    });
  },
}).catch(console.error);
