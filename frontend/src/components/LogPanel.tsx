import { useCallback, useEffect, useState } from 'react';

import { api, type InferenceLog } from '../lib/api';
import './logpanel.css';

interface LogPanelProps {
  conversationId: string | null;
  /** Bumped by the parent after each turn so the panel refetches. */
  refreshKey: number;
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function LogPanel({ conversationId, refreshKey }: LogPanelProps) {
  const [logs, setLogs] = useState<InferenceLog[]>([]);
  const [isOpen, setIsOpen] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!conversationId) {
      setLogs([]);
      return;
    }
    try {
      setLogs(await api.listLogs(conversationId));
    } catch {
      // The panel is diagnostic; a fetch failure shouldn't disrupt chat.
      setLogs([]);
    }
  }, [conversationId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  return (
    <section
      className={`logpanel${isOpen ? '' : ' logpanel--collapsed'}`}
      aria-label="Inference logs"
    >
      <header className="logpanel__head">
        <h2 className="logpanel__title">
          Inference logs
          {logs.length > 0 && <span className="logpanel__count">{logs.length}</span>}
        </h2>
        <button
          className="logpanel__toggle"
          onClick={() => setIsOpen((open) => !open)}
          aria-expanded={isOpen}
        >
          {isOpen ? 'Hide' : 'Show'}
        </button>
      </header>

      {isOpen && (
        <div className="logpanel__body">
          {logs.length === 0 ? (
            <p className="logpanel__empty">
              {conversationId
                ? 'No logs captured yet for this conversation.'
                : 'Select or start a conversation.'}
            </p>
          ) : (
            <ul className="logpanel__list">
              {logs.map((log) => (
                <LogRow
                  key={log.id}
                  log={log}
                  isExpanded={expandedId === log.id}
                  onToggle={() =>
                    setExpandedId((current) => (current === log.id ? null : log.id))
                  }
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

interface LogRowProps {
  log: InferenceLog;
  isExpanded: boolean;
  onToggle: () => void;
}

/**
 * One log entry. Collapsed shows the at-a-glance signals; expanded reveals the
 * full stored record, including the redacted previews — which is where the
 * redaction and the session linkage are actually visible.
 */
function LogRow({ log, isExpanded, onToggle }: LogRowProps) {
  return (
    <li className={`logrow${isExpanded ? ' logrow--expanded' : ''}`}>
      <button
        className="logrow__summary"
        onClick={onToggle}
        aria-expanded={isExpanded}
        aria-label={`${log.status} call, ${log.latency_ms ?? 'unknown'} milliseconds. Toggle details`}
      >
        <div className="logrow__top">
          <span className={`badge badge--${log.status}`}>{log.status}</span>
          <span className="logrow__latency">
            {log.latency_ms !== null ? `${log.latency_ms} ms` : '—'}
          </span>
        </div>
        <div className="logrow__model" title={`${log.provider} · ${log.model}`}>
          {log.provider} · {log.model}
        </div>
        <div className="logrow__meta">
          <dl className="logrow__tokens">
            <div>
              <dt>in</dt>
              <dd>{log.prompt_tokens ?? '—'}</dd>
            </div>
            <div>
              <dt>out</dt>
              <dd>{log.completion_tokens ?? '—'}</dd>
            </div>
          </dl>
          <span className="logrow__chevron" aria-hidden="true">
            {isExpanded ? '▲' : '▼'}
          </span>
        </div>
      </button>

      {log.error_message && <p className="logrow__error">{log.error_message}</p>}

      {isExpanded && (
        <div className="logrow__details">
          <Field label="Log ID" value={log.id} mono />
          <Field label="Conversation" value={log.conversation_id} mono />
          <Field label="Message" value={log.message_id ?? '—'} mono />
          <Field label="Model" value={log.model} mono />
          <Field label="Recorded" value={formatTimestamp(log.created_at)} />

          <Preview label="Input preview" text={log.input_preview} />
          <Preview label="Output preview" text={log.output_preview} />

          <p className="logrow__note">
            Previews are truncated and PII-redacted before storage.
          </p>
        </div>
      )}
    </li>
  );
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="field">
      <span className="field__label">{label}</span>
      <span className={`field__value${mono ? ' field__value--mono' : ''}`}>{value}</span>
    </div>
  );
}

function Preview({ label, text }: { label: string; text: string | null }) {
  return (
    <div className="preview">
      <span className="field__label">{label}</span>
      {text ? (
        <pre className="preview__text">{text}</pre>
      ) : (
        <span className="preview__empty">—</span>
      )}
    </div>
  );
}
