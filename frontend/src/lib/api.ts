/** Typed client for the backend API. Single place that knows about transport. */

const API_URL = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000';

export interface Conversation {
  id: string;
  title: string | null;
  status: string;
  created_at: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
}

export interface SendMessageResponse {
  user_message: Message;
  assistant_message: Message;
}

export interface InferenceLog {
  id: string;
  conversation_id: string;
  message_id: string | null;
  provider: string;
  model: string;
  latency_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  status: 'success' | 'error';
  error_message: string | null;
  created_at: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });

  if (!response.ok) {
    // Surface the server's detail so provider failures are legible in the UI
    // rather than collapsing into a generic "something went wrong".
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      // Non-JSON error body; keep the status-based message.
    }
    throw new Error(detail);
  }

  return response.json() as Promise<T>;
}

export const api = {
  listConversations: () => request<Conversation[]>('/conversations'),

  createConversation: () =>
    request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({}) }),

  getMessages: (conversationId: string) =>
    request<Message[]>(`/conversations/${conversationId}/messages`),

  sendMessage: (conversationId: string, content: string) =>
    request<SendMessageResponse>(`/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    }),

  listLogs: (conversationId?: string) =>
    request<InferenceLog[]>(
      conversationId ? `/logs?conversation_id=${conversationId}` : '/logs',
    ),
};
