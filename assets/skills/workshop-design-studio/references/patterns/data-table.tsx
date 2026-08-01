/**
 * Data table — tabular data with sensible density, alignment, and filtering.
 *
 * Two variants. Reach for the first one.
 *
 * 1. `DataTableCard` — AppKit's `DataTable`, which auto-generates columns from
 *    the query result and handles fetching, loading, error, empty, filtering,
 *    sorting, and pagination on its own. Do not hand-roll any of that.
 *    `parameters` is required even when the query takes none.
 *
 * 2. `StaticTable` — the primitives, for data you already hold in memory
 *    (client-side state, a game leaderboard, a computed list).
 *
 * Why they look designed:
 *  - numbers are right-aligned and use tabular figures, so columns line up;
 *  - headers are small, uppercase, and quiet — the data is the content;
 *  - status is a Badge with a word in it, never a bare coloured dot;
 *  - row height is comfortable rather than default-cramped.
 */
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  DataTable,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@databricks/appkit-ui/react';

export function DataTableCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">Deliveries</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          queryKey="recent_deliveries"
          parameters={{}}
          filterColumn="destination"
          filterPlaceholder="Filter by destination..."
          pageSize={10}
          // Format for a reader here (or in SQL) rather than shipping raw values.
          transform={(rows: Record<string, unknown>[]) =>
            rows.map((row) => ({
              ...row,
              delay_minutes: `${Number(row.delay_minutes)} min`,
            }))
          }
        />
      </CardContent>
    </Card>
  );
}

type Delivery = {
  id: string;
  destination: string;
  driver: string;
  delayMinutes: number;
  status: 'on-time' | 'late' | 'delivered';
};

const ROWS: Delivery[] = [
  { id: 'D-4821', destination: 'Manchester', driver: 'A. Osei', delayMinutes: 0, status: 'on-time' },
  { id: 'D-4822', destination: 'Leeds', driver: 'R. Kaur', delayMinutes: 18, status: 'late' },
  { id: 'D-4823', destination: 'Bristol', driver: 'J. Whyte', delayMinutes: 0, status: 'delivered' },
];

const STATUS_VARIANT: Record<Delivery['status'], 'default' | 'secondary' | 'destructive'> = {
  'on-time': 'secondary',
  late: 'destructive',
  delivered: 'default',
};

export function StaticTable() {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Ref</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Destination</TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Driver</TableHead>
          {/* Numbers right-align so the eye can compare down the column. */}
          <TableHead className="text-right text-xs font-medium uppercase tracking-wide">
            Delay
          </TableHead>
          <TableHead className="text-xs font-medium uppercase tracking-wide">Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {ROWS.map((row) => (
          <TableRow key={row.id}>
            <TableCell className="font-mono text-xs text-muted-foreground">{row.id}</TableCell>
            <TableCell className="font-medium">{row.destination}</TableCell>
            <TableCell className="text-muted-foreground">{row.driver}</TableCell>
            <TableCell className="text-right tabular-nums">
              {row.delayMinutes === 0 ? '—' : `${row.delayMinutes} min`}
            </TableCell>
            <TableCell>
              {/* The word is the signal; the colour only reinforces it. */}
              <Badge variant={STATUS_VARIANT[row.status]}>{row.status}</Badge>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
