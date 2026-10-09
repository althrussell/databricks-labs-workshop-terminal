import { createApp, lakebase, server } from '@databricks/appkit';
import { setupOrderRoutes } from './routes/lakebase/order-routes';

createApp({
  plugins: [
    lakebase(),
    server(),
  ],
  async onPluginsReady(appkit) {
    await setupOrderRoutes(appkit);
  },
}).catch(console.error);
