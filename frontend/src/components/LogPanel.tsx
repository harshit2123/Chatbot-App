import { useCallback, useEffect, useState } from 'react';

import { api, type InferenceLog } from '../lib/api';
import './logpanel.css';

interface Props {
  conversationId: string | null;
  /** Bumped by the parent after each turn so the panel refetches. */
  refreshKey: number;
}

export function LogPanel({ conversationId, refreshKey }: Props) {
  const [logs, setLogs] = useState<InferenceLog[]>([]);
  const [isOpen, setIsOpen] = useState(true);

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
    <section className={`logpanel${isOpen ? '' : ' logpanel--collapsed'}`} aria-label="Inference logs">
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
                <li key={log.id} className="logrow">
                  <div className="logrow__top">
                    <span className={`badge badge--${log.status}`}>{log.status}</span>
                    <span className="logrow__latency">
                      {log.latency_ms !== null ? `${log.latency_ms} ms` : '—'}
                    </span>
                  </div>
                  <div className="logrow__model" title={`${log.provider} · ${log.model}`}>
                    {log.provider} · {log.model}
                  </div>
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
                  {log.error_message && <p className="logrow__error">{log.error_message}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
