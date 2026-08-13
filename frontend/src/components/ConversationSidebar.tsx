import type { Conversation } from '../lib/api';
import './sidebar.css';

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function ConversationSidebar({ conversations, activeId, onSelect, onNew }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar__head">
        <div className="sidebar__brand">
          <span className="sidebar__mark" aria-hidden="true" />
          <span>Inference Console</span>
        </div>
        <button className="sidebar__new" onClick={onNew}>
          New chat
        </button>
      </div>

      <nav className="sidebar__list" aria-label="Conversations">
        {conversations.length === 0 ? (
          <p className="sidebar__empty">No conversations yet.</p>
        ) : (
          <ul>
            {conversations.map((conversation) => {
              const isActive = conversation.id === activeId;
              return (
                <li key={conversation.id}>
                  <button
                    className={`sidebar__item${isActive ? ' sidebar__item--active' : ''}`}
                    onClick={() => onSelect(conversation.id)}
                    aria-current={isActive ? 'true' : undefined}
                  >
                    <span className="sidebar__title">
                      {conversation.title ?? 'Untitled conversation'}
                    </span>
                    <span className="sidebar__meta">{formatDate(conversation.created_at)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </nav>
    </aside>
  );
}
