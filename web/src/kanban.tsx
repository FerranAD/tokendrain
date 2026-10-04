import { useEffect, useState } from 'react';
import { mutate, useAction, useResource } from './api';
import type { Task, TaskColumn } from './types';
import { ActionNotice, ErrorNotice, Loading } from './ui';

const columns: { id: TaskColumn; name: string }[] = [
  { id: 'backlog', name: 'Backlog' },
  { id: 'todo', name: 'Todo' },
  { id: 'in_progress', name: 'In progress' },
  { id: 'done', name: 'Done' },
];

export function Kanban({ id }: { id: string }) {
  const resource = useResource<Task[]>(`/projects/${id}/tasks`);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [editing, setEditing] = useState<Partial<Task> | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [target, setTarget] = useState<{ column: TaskColumn; position: number } | null>(null);
  const action = useAction();
  useEffect(() => {
    if (resource.data && !dragging) setTasks(resource.data);
  }, [resource.data]);
  const move = (taskId: string, column: TaskColumn, position: number) => {
    const original = tasks;
    const task = tasks.find((t) => t.id === taskId);
    if (!task) return;
    const others = tasks.filter((t) => t.id !== taskId);
    const ordered = others
      .filter((t) => t.column === column)
      .sort((a, b) => a.position - b.position);
    ordered.splice(position, 0, { ...task, column, position });
    setTasks([
      ...others.filter((t) => t.column !== column),
      ...ordered.map((t, i) => ({ ...t, position: i })),
    ]);
    void action.run(async () => {
      try {
        const updated = await mutate<Task[]>(`/projects/${id}/tasks`, 'POST', [
          { id: taskId, column, position },
        ]);
        setTasks(updated);
      } catch (error) {
        setTasks(original);
        throw error;
      }
    });
  };
  return (
    <>
      <div className="row between wrap">
        <p className="muted small">
          Drag to move or reorder. Backlog needs your approval; agents work In progress, then Todo.
        </p>
        <button
          className="primary"
          onClick={() => setEditing({ title: '', description: '', column: 'todo' })}
        >
          + Add task
        </button>
      </div>
      <ErrorNotice error={resource.error} />
      <ActionNotice {...action} />
      {!resource.data && <Loading />}
      {editing && (
        <section className="panel task-editor">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action.run(async () => {
                await mutate(`/projects/${id}/tasks`, 'POST', [editing]);
                setEditing(null);
              });
            }}
          >
            <h3>{editing.id ? 'Edit task' : 'New task'}</h3>
            <label>
              Title
              <input
                autoFocus
                required
                maxLength={300}
                value={editing.title || ''}
                onChange={(e) => setEditing({ ...editing, title: e.target.value })}
              />
            </label>
            <label>
              Details
              <textarea
                rows={4}
                value={editing.description || ''}
                onChange={(e) => setEditing({ ...editing, description: e.target.value })}
              />
            </label>
            <label>
              Column
              <select
                value={editing.column}
                onChange={(e) => setEditing({ ...editing, column: e.target.value as TaskColumn })}
              >
                {columns.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="row wrap">
              <button className="primary" disabled={action.busy}>
                Save task
              </button>
              <button type="button" onClick={() => setEditing(null)}>
                Cancel
              </button>
              {editing.id && (
                <button
                  type="button"
                  className="danger"
                  disabled={action.busy}
                  onClick={() => {
                    if (window.confirm('Delete this task?'))
                      void action.run(async () => {
                        await mutate(`/projects/${id}/tasks/${editing.id}`, 'DELETE');
                        setEditing(null);
                      });
                  }}
                >
                  Delete
                </button>
              )}
            </div>
          </form>
        </section>
      )}
      <div className="kanban">
        {columns.map((column) => {
          const cards = tasks
            .filter((t) => t.column === column.id)
            .sort((a, b) => a.position - b.position);
          return (
            <section
              className={`kanban-column ${dragging ? 'accept-drop' : ''}`}
              key={column.id}
              onDragOver={(e) => {
                e.preventDefault();
                setTarget({ column: column.id, position: cards.length });
              }}
              onDrop={(e) => {
                e.preventDefault();
                if (dragging && target) {
                  const sourceIndex = cards.findIndex((t) => t.id === dragging);
                  move(
                    dragging,
                    target.column,
                    target.position - (sourceIndex >= 0 && sourceIndex < target.position ? 1 : 0),
                  );
                }
                setDragging(null);
                setTarget(null);
              }}
            >
              <h3>
                {column.name}
                <span className="count">{tasks.filter((t) => t.column === column.id).length}</span>
              </h3>
              {cards.map((task, position) => (
                <div
                  key={task.id}
                  onDragOver={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const bounds = e.currentTarget.getBoundingClientRect();
                    setTarget({
                      column: column.id,
                      position: position + (e.clientY > bounds.top + bounds.height / 2 ? 1 : 0),
                    });
                  }}
                >
                  {target?.column === column.id && target.position === position && (
                    <div className="drop-marker" />
                  )}
                  <article
                    className="task-card"
                    style={{ opacity: task.id === dragging ? 0.3 : 1 }}
                    draggable={!action.busy}
                    onDragStart={(e) => {
                      e.dataTransfer.setData('text/plain', task.id);
                      e.dataTransfer.effectAllowed = 'move';
                      setDragging(task.id);
                    }}
                    onDragEnd={() => {
                      setDragging(null);
                      setTarget(null);
                    }}
                  >
                    <div className="row between">
                      <strong>{task.title}</strong>
                      {task.origin === 'agent' && <span className="ai-tag">AI</span>}
                    </div>
                    {task.description && <p>{task.description}</p>}
                    <button
                      className="quiet tiny"
                      onClick={() =>
                        setEditing({
                          id: task.id,
                          title: task.title,
                          description: task.description,
                          column: task.column,
                        })
                      }
                    >
                      Edit / move
                    </button>
                  </article>
                </div>
              ))}
              {target?.column === column.id && target.position === cards.length && (
                <div className="drop-marker" />
              )}
              {!cards.length && (
                <span className="muted tiny">{dragging ? 'Drop here' : 'No tasks'}</span>
              )}
              <button
                className="quiet small"
                onClick={() => setEditing({ title: '', description: '', column: column.id })}
              >
                + Add task
              </button>
            </section>
          );
        })}
      </div>
    </>
  );
}
