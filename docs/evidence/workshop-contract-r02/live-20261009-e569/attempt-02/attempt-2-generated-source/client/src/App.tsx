import { OrdersPage } from './pages/orders/OrdersPage';

export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-4 md:px-8">
          <span className="text-base font-semibold tracking-tight">
            Order<span className="text-primary">.</span>board
          </span>
        </div>
      </header>
      <main>
        <OrdersPage />
      </main>
    </div>
  );
}
