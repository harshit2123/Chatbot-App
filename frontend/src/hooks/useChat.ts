import { useCallback, useEffect, useState } from 'react';

import { api, type Conversation, type Message } from '../lib/api';

/**
 * Owns conversation list, active conversation, and turn sending.
 *
 * The user message is rendered optimistically, then reconciled against the
 * server's persisted rows so ids and timestamps are authoritative.
 */
export function useChat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  /** Resume: hydrate full history for a stored conversation. */
  const selectConversation = useCallback(async (conversationId: string) => {
    setActiveId(conversationId);
    setError(null);
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
      return conversation.id;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create conversation');
      return null;
    }
  }, []);

  const sendMessage = useCallback(
    async (content: string) => {
      const trimmed = content.trim();
      if (!trimmed || isSending) return;

      // Create a conversation lazily so the user can type first.
      let conversationId = activeId;
      if (!conversationId) {
        conversationId = await startNewConversation();
        if (!conversationId) return;
      }

      setError(null);
      setIsSending(true);

      const optimistic: Message = {
        id: `pending-${crypto.randomUUID()}`,
        conversation_id: conversationId,
        role: 'user',
        content: trimmed,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimistic]);

      try {
        const result = await api.sendMessage(conversationId, trimmed);
        setMessages((prev) => [
          ...prev.filter((m) => m.id !== optimistic.id),
          result.user_message,
          result.assistant_message,
        ]);
        // Title is derived server-side from the first message.
        void refreshConversations();
      } catch (err) {
        // Drop the optimistic message: the server may have persisted it, so
        // reload rather than guess at local state.
        setError(err instanceof Error ? err.message : 'Failed to send message');
        try {
          setMessages(await api.getMessages(conversationId));
        } catch {
          setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
        }
      } finally {
        setIsSending(false);
      }
    },
    [activeId, isSending, refreshConversations, startNewConversation],
  );

  return {
    conversations,
    activeId,
    messages,
    isSending,
    isLoadingHistory,
    error,
    selectConversation,
    startNewConversation,
    sendMessage,
    dismissError: () => setError(null),
  };
}
