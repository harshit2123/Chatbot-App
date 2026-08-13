import { useEffect, useState } from 'react';

import { Composer } from './components/Composer';
import { ConversationSidebar } from './components/ConversationSidebar';
import { LogPanel } from './components/LogPanel';
import { MessageList } from './components/MessageList';
import { useChat } from './hooks/useChat';
import { Dashboard } from './pages/Dashboard';
import './app.css';

type View = 'chat' | 'dashboard';

export default function App() {
  const {
    conversations,
    activeId,
    messages,
    streamingText,
    isStreaming,
    isLoadingHistory,
    error,
    selectConversation,
    startNewConversation,
    sendMessage,
    cancelGeneration,
    renameConversation,
    deleteConversation,
    dismissError,
  } = useChat();

  const [view, setView] = useState<View>('chat');

  // Refetch logs once a turn settles, since the log row is written through the
  // ingestion path rather than returned with the response.
  const [logRefreshKey, setLogRefreshKey] = useState(0);
  useEffect(() => {
    if (!isStreaming) setLogRefreshKey((key) => key + 1);
  }, [isStreaming, messages.length]);

  const activeTitle =
    conversations.find((conversation) => conversation.id === activeId)?.title ?? null;

  return (
    <div className="app">
      <ConversationSidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={(id) => {
          setView('chat');
          void selectConversation(id);
        }}
        onNew={() => {
          setView('chat');
          void startNewConversation();
        }}
        onRename={renameConversation}
        onDelete={deleteConversation}
        view={view}
        onShowDashboard={() => setView('dashboard')}
      />

      {view === 'dashboard' ? (
        <Dashboard />
      ) : (
        <>
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
              streamingText={streamingText}
              isStreaming={isStreaming}
              isLoadingHistory={isLoadingHistory}
            />
            <Composer
              onSend={sendMessage}
              onCancel={cancelGeneration}
              disabled={isStreaming}
            />
          </main>

          <LogPanel conversationId={activeId} refreshKey={logRefreshKey} />
        </>
      )}
    </div>
  );
}
