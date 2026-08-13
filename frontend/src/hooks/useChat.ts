import { useCallback, useEffect, useRef, useState } from 'react';

import { api, streamMessage, type Conversation, type Message } from '../lib/api';

/**
 * Owns conversation list, active conversation, and streaming turns.
 *
 * Cancellation is two-sided: AbortController closes the client's connection,
 * and POST /cancel sets a server flag so the generation actually stops instead
 * of continuing to burn tokens after the browser walked away.
 */
export function useChat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streamingText, setStreamingText] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load conversations');
    }
  }, []);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  // Abort any in-flight stream when the component unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  /** Resume: hydrate full history for a stored conversation. */
  const selectConversation = useCallback(async (conversationId: string) => {
    abortRef.current?.abort();
    setActiveId(conversationId);
    setError(null);
    setStreamingText('');
    setIsLoadingHistory(true);
    try {
      setMessages(await api.getMessages(conversationId));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load conversation');
      setMessages([]);
    } finally {
      setIsLoadingHistory(false);
    }
  }, []);

  const startNewConversation = useCallback(async () => {
    setError(null);
    try {
      const conversation = await api.createConversation();
      setConversations((prev) => [conversation, ...prev]);
      setActiveId(conversation.id);
      setMessages([]);
      setStreamingText('');
      return conversation.id;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create conversation');
      return null;
    }
  }, []);

  const cancelGeneration = useCallback(async () => {
    if (!activeId || !isStreaming) return;
    try {
      // Tell the server first: aborting locally alone would leave the
      // generation running server-side.
      await api.cancelGeneration(activeId);
    } catch {
      // Even if the signal fails, still drop the client connection.
    }
    abortRef.current?.abort();
  }, [activeId, isStreaming]);

  const sendMessage = useCallback(
    async (content: string) => {
      const trimmed = content.trim();
      if (!trimmed || isStreaming) return;

      let conversationId = activeId;
      if (!conversationId) {
        conversationId = await startNewConversation();
        if (!conversationId) return;
      }

      setError(null);
      setIsStreaming(true);
      setStreamingText('');

      const controller = new AbortController();
      abortRef.current = controller;

      const optimistic: Message = {
        id: `pending-${crypto.randomUUID()}`,
        conversation_id: conversationId,
        role: 'user',
        content: trimmed,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimistic]);

      let accumulated = '';

      try {
        for await (const event of streamMessage(conversationId, trimmed, controller.signal)) {
          switch (event.type) {
            case 'start':
              // Swap the optimistic row for the server's persisted one.
              setMessages((prev) =>
                prev.map((m) => (m.id === optimistic.id ? event.userMessage : m)),
              );
              break;
            case 'delta':
              accumulated += event.content;
              setStreamingText(accumulated);
              break;
            case 'done':
            case 'cancelled':
              if (event.assistantMessage) {
                setMessages((prev) => [...prev, event.assistantMessage!]);
              }
              setStreamingText('');
              break;
            case 'error':
              setError(event.detail);
              setStreamingText('');
              break;
          }
        }
        void refreshConversations();
      } catch (err) {
        if (controller.signal.aborted) {
          // User-initiated cancel. Reload so the partial reply the server
          // persisted is reflected accurately.
          try {
            setMessages(await api.getMessages(conversationId));
          } catch {
            /* keep what is on screen */
          }
        } else {
          setError(err instanceof Error ? err.message : 'Failed to send message');
          try {
            setMessages(await api.getMessages(conversationId));
          } catch {
            setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
          }
        }
      } finally {
        setIsStreaming(false);
        setStreamingText('');
        abortRef.current = null;
      }
    },
    [activeId, isStreaming, refreshConversations, startNewConversation],
  );

  return {
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
    dismissError: () => setError(null),
  };
}
