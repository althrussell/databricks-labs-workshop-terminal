export const formatCurrency = (value: number | string): string =>
  `$${Number(value).toFixed(2)}`;

export const formatDueDate = (iso: string): string =>
  new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

export const daysOverdue = (iso: string): number => {
  const diffMs = Date.now() - new Date(iso).getTime();
  return Math.max(1, Math.round(diffMs / (1000 * 60 * 60 * 24)));
};
