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
  status: 'success' | 'error' | 'cancelled';
  error_message: string | null;
  input_preview: string | null;
  output_preview: string | null;
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

export interface MetricsSummary {
  window_minutes: number;
  total_calls: number;
  error_count: number;
  error_rate: number;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
  total_prompt_tokens: number;
  total_completion_tokens: number;
}

export interface LatencyPoint {
  bucket: string;
  avg_latency_ms: number;
  max_latency_ms: number;
  count: number;
}

export interface ErrorPoint {
  bucket: string;
  total: number;
  errors: number;
  error_rate: number;
}

export interface ThroughputPoint {
  bucket: string;
  count: number;
  calls_per_minute: number;
}

export interface ProviderBreakdown {
  provider: string;
  model: string;
  count: number;
  avg_latency_ms: number | null;
  error_count: number;
}

/** Events emitted by the SSE chat stream. */
export type StreamEvent =
  | { type: 'start'; userMessage: Message }
  | { type: 'delta'; content: string }
  | { type: 'done'; assistantMessage: Message | null }
  | { type: 'cancelled'; assistantMessage: Message | null }
  | { type: 'error'; detail: string };

/**
 * POST a message and yield SSE frames as they arrive.
 *
 * Uses fetch + ReadableStream rather than EventSource because EventSource
 * cannot issue a POST with a JSON body.
 */
export async function* streamMessage(
  conversationId: string,
  content: string,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_URL}/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Stream failed (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line; a partial frame stays buffered.
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';

    for (const frame of frames) {
      if (!frame.trim()) continue;

      let eventName = '';
      let dataLine = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event: ')) eventName = line.slice(7);
        else if (line.startsWith('data: ')) dataLine = line.slice(6);
      }
      if (!eventName || !dataLine) continue;

      const data = JSON.parse(dataLine);
      switch (eventName) {
        case 'start':
          yield { type: 'start', userMessage: data.user_message };
          break;
        case 'delta':
          yield { type: 'delta', content: data.content };
          break;
        case 'done':
          yield { type: 'done', assistantMessage: data.assistant_message };
          break;
        case 'cancelled':
          yield { type: 'cancelled', assistantMessage: data.assistant_message };
          break;
        case 'error':
          yield { type: 'error', detail: data.detail };
          break;
      }
    }
  }
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

  renameConversation: (conversationId: string, title: string) =>
    request<Conversation>(`/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),

  deleteConversation: async (conversationId: string): Promise<void> => {
    // 204 No Content, so there is no body to parse.
    const response = await fetch(`${API_URL}/conversations/${conversationId}`, {
      method: 'DELETE',
    });
    if (!response.ok) throw new Error(`Delete failed (${response.status})`);
  },

  cancelGeneration: (conversationId: string) =>
    request<{ status: string }>(`/conversations/${conversationId}/cancel`, {
      method: 'POST',
    }),

  metricsSummary: (windowMinutes: number) =>
    request<MetricsSummary>(`/metrics/summary?window_minutes=${windowMinutes}`),

  metricsLatency: (windowMinutes: number) =>
    request<LatencyPoint[]>(`/metrics/latency?window_minutes=${windowMinutes}`),

  metricsErrors: (windowMinutes: number) =>
    request<ErrorPoint[]>(`/metrics/errors?window_minutes=${windowMinutes}`),

  metricsThroughput: (windowMinutes: number) =>
    request<ThroughputPoint[]>(`/metrics/throughput?window_minutes=${windowMinutes}`),

  metricsProviders: (windowMinutes: number) =>
    request<ProviderBreakdown[]>(`/metrics/providers?window_minutes=${windowMinutes}`),
};
