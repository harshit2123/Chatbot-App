import { useCallback, useEffect, useState } from 'react';

import { BarChart, LineChart, type ChartPoint } from '../components/Charts';
import {
  api,
  type ErrorPoint,
  type LatencyPoint,
  type MetricsSummary,
  type ProviderBreakdown,
  type ThroughputPoint,
} from '../lib/api';
import './dashboard.css';

const WINDOWS = [
  { label: '1h', minutes: 60 },
  { label: '24h', minutes: 60 * 24 },
  { label: '7d', minutes: 60 * 24 * 7 },
];

const REFRESH_INTERVAL_MS = 10_000;

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatMs(value: number | null): string {
  if (value === null) return '—';
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

export function Dashboard() {
  const [windowMinutes, setWindowMinutes] = useState(WINDOWS[0].minutes);
  const [summary, setSummary] = useState<MetricsSummary | null>(null);
  const [latency, setLatency] = useState<LatencyPoint[]>([]);
  const [errors, setErrors] = useState<ErrorPoint[]>([]);
  const [throughput, setThroughput] = useState<ThroughputPoint[]>([]);
  const [providers, setProviders] = useState<ProviderBreakdown[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      // Parallel, not sequential: five independent queries shouldn't waterfall.
      const [summaryData, latencyData, errorData, throughputData, providerData] =
        await Promise.all([
          api.metricsSummary(windowMinutes),
          api.metricsLatency(windowMinutes),
          api.metricsErrors(windowMinutes),
          api.metricsThroughput(windowMinutes),
          api.metricsProviders(windowMinutes),
        ]);

      setSummary(summaryData);
      setLatency(latencyData);
      setErrors(errorData);
      setThroughput(throughputData);
      setProviders(providerData);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load metrics');
    }
  }, [windowMinutes]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const latencyPoints: ChartPoint[] = latency.map((point) => ({
    label: formatTime(point.bucket),
    value: point.avg_latency_ms,
  }));

  const errorPoints: ChartPoint[] = errors.map((point) => ({
    label: formatTime(point.bucket),
    value: point.error_rate * 100,
  }));

  const throughputPoints: ChartPoint[] = throughput.map((point) => ({
    label: formatTime(point.bucket),
    value: point.calls_per_minute,
  }));

  return (
    <div className="dashboard">
      <header className="dashboard__head">
        <div>
          <h1 className="dashboard__title">Observability</h1>
          <p className="dashboard__subtitle">
            Aggregated from <code>inference_logs</code> — every model call, including failures.
          </p>
        </div>
        <div className="dashboard__windows" role="group" aria-label="Time window">
          {WINDOWS.map((option) => (
            <button
              key={option.minutes}
              className={`window-btn${
                option.minutes === windowMinutes ? ' window-btn--active' : ''
              }`}
              onClick={() => setWindowMinutes(option.minutes)}
              aria-pressed={option.minutes === windowMinutes}
            >
              {option.label}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <div className="dashboard__error" role="alert">
          {error}
        </div>
      )}

      <section className="tiles" aria-label="Summary">
        <Tile label="Total calls" value={summary ? String(summary.total_calls) : '—'} />
        <Tile
          label="Error rate"
          value={summary ? `${(summary.error_rate * 100).toFixed(1)}%` : '—'}
          tone={summary && summary.error_rate > 0 ? 'danger' : 'neutral'}
        />
        <Tile label="Avg latency" value={formatMs(summary?.avg_latency_ms ?? null)} />
        <Tile label="p95 latency" value={formatMs(summary?.p95_latency_ms ?? null)} />
        <Tile
          label="Tokens in / out"
          value={
            summary
              ? `${summary.total_prompt_tokens} / ${summary.total_completion_tokens}`
              : '—'
          }
        />
      </section>

      <section className="panels">
        <Panel title="Average latency" hint="ms per bucket">
          <LineChart points={latencyPoints} color="var(--color-accent)" unit=" ms" />
        </Panel>

        <Panel title="Error rate" hint="% of calls failing">
          <LineChart points={errorPoints} color="var(--color-danger)" unit="%" />
        </Panel>

        <Panel title="Throughput" hint="calls per minute">
          <BarChart points={throughputPoints} color="var(--color-success)" />
        </Panel>

        <Panel title="By provider and model" hint="resolved at call time">
          {providers.length === 0 ? (
            <p className="panel__empty">No calls in this window.</p>
          ) : (
            <table className="provider-table">
              <thead>
                <tr>
                  <th scope="col">Provider</th>
                  <th scope="col">Model</th>
                  <th scope="col">Calls</th>
                  <th scope="col">Avg</th>
                  <th scope="col">Errors</th>
                </tr>
              </thead>
              <tbody>
                {providers.map((row) => (
                  <tr key={`${row.provider}-${row.model}`}>
                    <td>{row.provider}</td>
                    <td className="provider-table__model">{row.model}</td>
                    <td className="provider-table__num">{row.count}</td>
                    <td className="provider-table__num">{formatMs(row.avg_latency_ms)}</td>
                    <td className="provider-table__num">
                      {row.error_count > 0 ? (
                        <span className="provider-table__errors">{row.error_count}</span>
                      ) : (
                        '0'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </section>
    </div>
  );
}

function Tile({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  tone?: 'neutral' | 'danger';
}) {
  return (
    <div className="tile">
      <span className="tile__label">{label}</span>
      <span className={`tile__value${tone === 'danger' ? ' tile__value--danger' : ''}`}>
        {value}
      </span>
    </div>
  );
}

function Panel({
  title,
  hint,
  children,
}: {
  title: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <header className="panel__head">
        <h2 className="panel__title">{title}</h2>
        <span className="panel__hint">{hint}</span>
      </header>
      {children}
    </section>
  );
}
