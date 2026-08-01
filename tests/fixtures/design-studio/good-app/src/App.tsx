import "./theme.css";

export function App() {
  const loading = false;
  const error: string | null = null;
  const items = ["Design", "Build", "Refine"];

  if (loading) return <main aria-busy="true">Loading…</main>;
  if (error) return <main role="alert">Error: {error}</main>;
  if (!items.length) return <main>Empty — create your first item.</main>;

  return (
    <main className="page">
      <header>
        <p className="eyebrow">Workshop Design Studio</p>
        <h1>Make the first draft feel like the fifth.</h1>
        <button type="button">Start creating</button>
      </header>
      <section aria-labelledby="principles">
        <h2 id="principles">Principles</h2>
        <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
      </section>
      <img src="/art-direction.svg" alt="" />
    </main>
  );
}
