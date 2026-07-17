export default function Home() {
  return (
    <main className="viewer-shell">
      <iframe
        className="viewer-frame"
        src="/viewer/index.html"
        title="Moke garden point-cloud viewer"
        allow="fullscreen"
      />
    </main>
  );
}
