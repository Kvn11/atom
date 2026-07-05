export function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  return (
    <div className="page">
      <button className="link" onClick={onBack}>← Runs</button>
      <h1>Run {runId}</h1>
      <div className="empty">Run console coming in Task 8.</div>
    </div>
  );
}
