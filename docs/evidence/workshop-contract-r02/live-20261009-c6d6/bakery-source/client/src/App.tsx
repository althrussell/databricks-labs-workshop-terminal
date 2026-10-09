import { createBrowserRouter, RouterProvider, Outlet } from 'react-router';
import { Croissant } from 'lucide-react';
import { OrdersPage } from './OrdersPage';

function Layout() {
  return (
    <div className="min-h-screen bg-background flex flex-col">
      <header className="border-b border-border bg-card px-4 py-4 md:px-6">
        <span className="flex items-center gap-2 text-base font-semibold tracking-tight text-foreground">
          <Croissant className="size-5 text-primary" aria-hidden="true" />
          The Order Board
        </span>
      </header>

      <main className="flex-1">
        <Outlet />
      </main>
    </div>
  );
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <OrdersPage /> },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
