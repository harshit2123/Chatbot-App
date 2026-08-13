import { useEffect, useState } from 'react';

import { Composer } from './components/Composer';
import { ConversationSidebar } from './components/ConversationSidebar';
import { LogPanel } from './components/LogPanel';
import { MessageList } from './components/MessageList';
import { useChat } from './hooks/useChat';
import './app.css';

export default function App() {
  const {
    conversations,
    activeId,
    messages,
    isSending,
    isLoadingHistory,
    error,
    selectConversation,
    startNewConversation,
    sendMessage,
    dismissError,
  } = useChat();

  // Refetch logs once a turn settles, since the log row is written by the
  // ingestion path rather than returned with the chat response.
  const [logRefreshKey, setLogRefreshKey] = useState(0);
  useEffect(() => {
    if (!isSending) setLogRefreshKey((key) => key + 1);
  }, [isSending, messages.length]);

  const activeTitle =
    conversations.find((conversation) => conversation.id === activeId)?.title ?? null;

  return (
    <div className="app">
      <ConversationSidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={selectConversation}
        onNew={startNewConversation}
      />

      <main className="app__main">
        <header className="app__header">
          <h1 className="app__title">{activeTitle ?? 'New conversation'}</h1>
        </header>

        {error && (
          <div className="app__error" role="alert">
            <span>{error}</span>
            <button onClick={dismissError} aria-label="Dismiss error">
              ×
            </button>
          </div>
        )}

        <MessageList
          messages={messages}
          isSending={isSending}
          isLoadingHistory={isLoadingHistory}
        />
        <Composer onSend={sendMessage} disabled={isSending} />
      </main>

      <LogPanel conversationId={activeId} refreshKey={logRefreshKey} />
    </div>
  );
}
