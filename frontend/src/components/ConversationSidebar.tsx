import { useEffect, useRef, useState, type KeyboardEvent } from 'react';

import type { Conversation } from '../lib/api';
import './sidebar.css';

interface ConversationSidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  view: 'chat' | 'dashboard';
  onShowDashboard: () => void;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function ConversationSidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onRename,
  onDelete,
  view,
  onShowDashboard,
}: ConversationSidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

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
        <button
          className={`sidebar__nav${view === 'dashboard' ? ' sidebar__nav--active' : ''}`}
          onClick={onShowDashboard}
          aria-current={view === 'dashboard' ? 'page' : undefined}
        >
          Observability
        </button>
      </div>

      <nav className="sidebar__list" aria-label="Conversations">
        {conversations.length === 0 ? (
          <p className="sidebar__empty">No conversations yet.</p>
        ) : (
          <ul>
            {conversations.map((conversation) => (
              <ConversationItem
                key={conversation.id}
                conversation={conversation}
                isActive={conversation.id === activeId}
                isEditing={editingId === conversation.id}
                isConfirmingDelete={confirmingId === conversation.id}
                onSelect={() => onSelect(conversation.id)}
                onStartEdit={() => {
                  setConfirmingId(null);
                  setEditingId(conversation.id);
                }}
                onSubmitEdit={(title) => {
                  onRename(conversation.id, title);
                  setEditingId(null);
                }}
                onCancelEdit={() => setEditingId(null)}
                onRequestDelete={() => {
                  setEditingId(null);
                  setConfirmingId(conversation.id);
                }}
                onConfirmDelete={() => {
                  onDelete(conversation.id);
                  setConfirmingId(null);
                }}
                onCancelDelete={() => setConfirmingId(null)}
              />
            ))}
          </ul>
        )}
      </nav>
    </aside>
  );
}

interface ConversationItemProps {
  conversation: Conversation;
  isActive: boolean;
  isEditing: boolean;
  isConfirmingDelete: boolean;
  onSelect: () => void;
  onStartEdit: () => void;
  onSubmitEdit: (title: string) => void;
  onCancelEdit: () => void;
  onRequestDelete: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
}

function ConversationItem({
  conversation,
  isActive,
  isEditing,
  isConfirmingDelete,
  onSelect,
  onStartEdit,
  onSubmitEdit,
  onCancelEdit,
  onRequestDelete,
  onConfirmDelete,
  onCancelDelete,
}: ConversationItemProps) {
  const title = conversation.title ?? 'Untitled conversation';
  const [draft, setDraft] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isEditing) {
      setDraft(title);
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [isEditing, title]);

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') onSubmitEdit(draft);
    if (event.key === 'Escape') onCancelEdit();
  };

  if (isEditing) {
    return (
      <li>
        <div className="sidebar__item sidebar__item--editing">
          <input
            ref={inputRef}
            className="sidebar__rename-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleKeyDown}
            // Committing on blur means clicking away saves rather than
            // silently discarding what was typed.
            onBlur={() => onSubmitEdit(draft)}
            aria-label="Conversation title"
          />
        </div>
      </li>
    );
  }

  return (
    <li>
      <div
        className={`sidebar__item${isActive ? ' sidebar__item--active' : ''}${
          isConfirmingDelete ? ' sidebar__item--confirming' : ''
        }`}
      >
        <button className="sidebar__select" onClick={onSelect} aria-current={isActive || undefined}>
          <span className="sidebar__title">{title}</span>
        </button>

        {isConfirmingDelete ? (
          // Deleting a conversation is irreversible, so it takes a second click.
          <span className="sidebar__confirm">
            <button
              className="sidebar__confirm-yes"
              onClick={onConfirmDelete}
              data-testid="confirm-delete"
              aria-label={`Confirm delete ${title}`}
            >
              Delete
            </button>
            <button className="sidebar__confirm-no" onClick={onCancelDelete}>
              Cancel
            </button>
          </span>
        ) : (
          <span className="sidebar__actions">
            <button
              className="sidebar__action"
              onClick={onStartEdit}
              data-testid="rename-button"
              aria-label={`Rename ${title}`}
              title="Rename"
            >
              <PencilIcon />
            </button>
            <button
              className="sidebar__action sidebar__action--danger"
              onClick={onRequestDelete}
              data-testid="delete-button"
              aria-label={`Delete ${title}`}
              title="Delete"
            >
              <TrashIcon />
            </button>
            <span className="sidebar__meta">{formatDate(conversation.created_at)}</span>
          </span>
        )}
      </div>
    </li>
  );
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path
        d="M11.5 2.5a1.4 1.4 0 0 1 2 2L5.8 12.2l-2.6.7.7-2.6 7.6-7.8Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path
        d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.2h5.8l.6-8.2"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
