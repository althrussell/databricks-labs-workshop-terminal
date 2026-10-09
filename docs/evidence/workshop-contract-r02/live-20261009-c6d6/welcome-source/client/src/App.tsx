import { Card, CardContent } from '@databricks/appkit-ui/react';

export default function App() {
  return (
    <main className="welcome-page">
      <section className="welcome-hero" aria-labelledby="welcome-title">
        <p className="welcome-eyebrow">Workshop demo</p>
        <h1 id="welcome-title">Hello, workshop!</h1>
        <p className="welcome-intro">
          This is a small demo page, ready to welcome you into the workshop.
        </p>

        <Card className="welcome-note">
          <CardContent className="welcome-note-content">
            <span className="welcome-note-mark" aria-hidden="true">
              ✦
            </span>
            <div>
              <h2>A simple place to begin</h2>
              <p>
                There are no data connections or actions here—just a friendly
                introduction to show what a workshop app can look like.
              </p>
            </div>
          </CardContent>
        </Card>
      </section>
    </main>
  );
}
