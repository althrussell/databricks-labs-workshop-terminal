/**
 * Form with validation states.
 *
 * Why it looks and behaves designed:
 *  - validates on blur and on submit, never on every keystroke — errors that
 *    appear while someone types their first character read as hostile;
 *  - the error sits next to its field, describes the fix rather than the
 *    violation, and is wired with aria-invalid + aria-describedby;
 *  - the submit button disables and says what it is doing, so nobody clicks
 *    twice into a duplicate record;
 *  - required is marked once, consistently, and explained.
 */
import { useState } from 'react';
import {
  Button,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@databricks/appkit-ui/react';

type Values = { name: string; plate: string; depot: string };
type Errors = Partial<Record<keyof Values, string>>;

function validate(values: Values): Errors {
  const errors: Errors = {};
  if (!values.name.trim()) errors.name = 'Give the vehicle a name your team will recognise.';
  if (!/^[A-Z0-9 ]{4,10}$/i.test(values.plate.trim())) {
    errors.plate = 'Use the plate as it appears on the vehicle, e.g. AB12 CDE.';
  }
  if (!values.depot) errors.depot = 'Choose the depot this vehicle runs from.';
  return errors;
}

export function VehicleForm({ onSubmit }: { onSubmit?: (values: Values) => Promise<void> }) {
  const [values, setValues] = useState<Values>({ name: '', plate: '', depot: '' });
  const [errors, setErrors] = useState<Errors>({});
  const [touched, setTouched] = useState<Partial<Record<keyof Values, boolean>>>({});
  const [submitting, setSubmitting] = useState(false);

  const set = (field: keyof Values) => (value: string) => {
    const next = { ...values, [field]: value };
    setValues(next);
    // Only re-validate a field the user has already left, so typing is quiet.
    if (touched[field]) setErrors(validate(next));
  };

  const blur = (field: keyof Values) => () => {
    setTouched((t) => ({ ...t, [field]: true }));
    setErrors(validate(values));
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const found = validate(values);
    setErrors(found);
    setTouched({ name: true, plate: true, depot: true });
    if (Object.keys(found).length > 0) return;

    setSubmitting(true);
    try {
      await onSubmit?.(values);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} noValidate className="max-w-md space-y-6">
      <div className="space-y-1.5">
        <h2 className="text-xl font-semibold tracking-tight">Add a vehicle</h2>
        <p className="text-sm text-muted-foreground">All three fields are required.</p>
      </div>

      <Field id="name" label="Name" error={errors.name} show={touched.name}>
        <Input
          id="name"
          value={values.name}
          placeholder="Depot van 4"
          onChange={(e) => set('name')(e.target.value)}
          onBlur={blur('name')}
          aria-invalid={touched.name && Boolean(errors.name)}
          aria-describedby={errors.name ? 'name-error' : undefined}
        />
      </Field>

      <Field id="plate" label="Registration" error={errors.plate} show={touched.plate}>
        <Input
          id="plate"
          value={values.plate}
          placeholder="AB12 CDE"
          onChange={(e) => set('plate')(e.target.value)}
          onBlur={blur('plate')}
          aria-invalid={touched.plate && Boolean(errors.plate)}
          aria-describedby={errors.plate ? 'plate-error' : undefined}
        />
      </Field>

      <Field id="depot" label="Depot" error={errors.depot} show={touched.depot}>
        {/* SelectItem value can never be "" — use a real sentinel if you need one. */}
        <Select
          value={values.depot || undefined}
          onValueChange={(value) => {
            setTouched((t) => ({ ...t, depot: true }));
            const next = { ...values, depot: value };
            setValues(next);
            setErrors(validate(next));
          }}
        >
          <SelectTrigger id="depot" aria-invalid={touched.depot && Boolean(errors.depot)}>
            <SelectValue placeholder="Choose a depot" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="manchester">Manchester</SelectItem>
            <SelectItem value="leeds">Leeds</SelectItem>
            <SelectItem value="bristol">Bristol</SelectItem>
          </SelectContent>
        </Select>
      </Field>

      <div className="flex items-center gap-3 pt-2">
        <Button type="submit" disabled={submitting}>
          {submitting ? 'Adding…' : 'Add vehicle'}
        </Button>
        <Button type="button" variant="ghost" disabled={submitting}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function Field({
  id,
  label,
  error,
  show,
  children,
}: {
  id: string;
  label: string;
  error?: string;
  show?: boolean;
  children: React.ReactNode;
}) {
  const visible = Boolean(show && error);
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {/* aria-live so a screen reader announces the error when it appears. */}
      <p
        id={`${id}-error`}
        aria-live="polite"
        className={visible ? 'text-sm text-destructive' : 'sr-only'}
      >
        {visible ? error : ''}
      </p>
    </div>
  );
}
