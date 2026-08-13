import { useEffect, useRef } from 'react';

import type { Message } from '../lib/api';
import './messages.css';

interface Props {
  messages: Message[];
  streamingText: string;
  isStreaming: boolean;
  isLoadingHistory: boolean;
}

export function MessageList({
  messages,
  streamingText,
  isStreaming,
  isLoadingHistory,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, isStreaming, streamingText]);

  if (isLoadingHistory) {
    return (
      <div className="messages messages--centered">
        <p className="messages__hint">Loading conversation…</p>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="messages messages--centered">
        <div className="messages__empty">
          <h2>Start a conversation</h2>
          <p>
            Every turn is instrumented — provider, model, latency, and token counts are
            captured and stored as an inference log.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="messages">
      <ol className="messages__list">
        {messages.map((message) => (
          <li key={message.id} className={`bubble bubble--${message.role}`}>
            <span className="bubble__role">
              {message.role === 'user' ? 'You' : 'Assistant'}
            </span>
            <div className="bubble__body">{message.content}</div>
          </li>
        ))}
        {isStreaming && (
          <li className="bubble bubble--assistant">
            <span className="bubble__role">Assistant</span>
            {streamingText ? (
              <div className="bubble__body" aria-live="polite">
                {streamingText}
                <span className="cursor" aria-hidden="true" />
              </div>
            ) : (
              <div className="bubble__body bubble__body--pending" aria-live="polite">
                <span className="dot" />
                <span className="dot" />
                <span className="dot" />
              </div>
            )}
          </li>
        )}
      </ol>
      <div ref={endRef} />
    </div>
  );
}
