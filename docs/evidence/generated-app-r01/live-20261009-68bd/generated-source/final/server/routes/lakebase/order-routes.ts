// For per-user connections (OBO) with Row-Level Security, see:
// https://developers.databricks.com/docs/appkit/v0/plugins/lakebase#on-behalf-of-obo--per-user-connections

import { z } from 'zod';
import { Application } from 'express';

interface AppKitWithLakebase {
  lakebase: {
    query(text: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  };
  server: {
    extend(fn: (app: Application) => void): void;
  };
}

const TABLE_EXISTS_SQL = `
  SELECT 1 FROM information_schema.tables
  WHERE table_schema = 'app' AND table_name = 'orders'
`;

const SETUP_SCHEMA_SQL = `CREATE SCHEMA IF NOT EXISTS app`;

const CREATE_TABLE_SQL = `
  CREATE TABLE IF NOT EXISTS app.orders (
    id SERIAL PRIMARY KEY,
    customer_name TEXT NOT NULL,
    item TEXT NOT NULL,
    due_date DATE NOT NULL,
    notes TEXT,
    packed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )
`;

const CreateOrderBody = z.object({
  customerName: z.string().min(1),
  item: z.string().min(1),
  dueDate: z.string().min(1),
  notes: z.string().optional(),
});

export async function setupOrderRoutes(appkit: AppKitWithLakebase) {
  try {
    const { rows } = await appkit.lakebase.query(TABLE_EXISTS_SQL);
    if (rows.length > 0) {
      console.log('[lakebase] Table app.orders already exists, skipping setup');
    } else {
      await appkit.lakebase.query(SETUP_SCHEMA_SQL);
      await appkit.lakebase.query(CREATE_TABLE_SQL);
      console.log('[lakebase] Created schema and table app.orders');
    }
  } catch (err) {
    console.warn('[lakebase] Database setup failed:', (err as Error).message);
    console.warn('[lakebase] Routes will be registered but may return errors');
    console.warn('[lakebase] See https://developers.databricks.com/docs/appkit/v0/plugins/lakebase#database-permissions for troubleshooting');
  }

  appkit.server.extend((app) => {
    app.get('/api/lakebase/orders', async (_req, res) => {
      try {
        const result = await appkit.lakebase.query(
          `SELECT id, customer_name, item, due_date, notes, packed, created_at
           FROM app.orders
           ORDER BY packed ASC, due_date ASC, created_at ASC`,
        );
        res.json(result.rows);
      } catch (err) {
        console.error('Failed to list orders:', err);
        res.status(500).json({ error: 'Failed to list orders' });
      }
    });

    app.post('/api/lakebase/orders', async (req, res) => {
      try {
        const parsed = CreateOrderBody.safeParse(req.body);
        if (!parsed.success) {
          res.status(400).json({ error: 'Customer, order, and due date are required' });
          return;
        }
        const { customerName, item, dueDate, notes } = parsed.data;
        const result = await appkit.lakebase.query(
          `INSERT INTO app.orders (customer_name, item, due_date, notes)
           VALUES ($1, $2, $3, $4)
           RETURNING id, customer_name, item, due_date, notes, packed, created_at`,
          [customerName.trim(), item.trim(), dueDate, notes?.trim() || null],
        );
        res.status(201).json(result.rows[0]);
      } catch (err) {
        console.error('Failed to create order:', err);
        res.status(500).json({ error: 'Failed to create order' });
      }
    });

    app.patch('/api/lakebase/orders/:id/packed', async (req, res) => {
      try {
        const id = parseInt(req.params.id, 10);
        if (isNaN(id)) {
          res.status(400).json({ error: 'Invalid id' });
          return;
        }
        const result = await appkit.lakebase.query(
          `UPDATE app.orders SET packed = NOT packed WHERE id = $1
           RETURNING id, customer_name, item, due_date, notes, packed, created_at`,
          [id],
        );
        if (result.rows.length === 0) {
          res.status(404).json({ error: 'Order not found' });
          return;
        }
        res.json(result.rows[0]);
      } catch (err) {
        console.error('Failed to update order:', err);
        res.status(500).json({ error: 'Failed to update order' });
      }
    });

    app.delete('/api/lakebase/orders/:id', async (req, res) => {
      try {
        const id = parseInt(req.params.id, 10);
        if (isNaN(id)) {
          res.status(400).json({ error: 'Invalid id' });
          return;
        }
        const result = await appkit.lakebase.query(
          'DELETE FROM app.orders WHERE id = $1 RETURNING id',
          [id],
        );
        if (result.rows.length === 0) {
          res.status(404).json({ error: 'Order not found' });
          return;
        }
        res.status(204).send();
      } catch (err) {
        console.error('Failed to delete order:', err);
        res.status(500).json({ error: 'Failed to delete order' });
      }
    });
  });
}
