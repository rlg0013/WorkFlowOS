export type WorkflowStatus = "pending" | "approved" | "rejected" | "executing" | "completed" | "failed";
export interface WorkflowAction { step: number; action: string; target: string; params: Record<string, unknown>; }
export interface WorkflowSpec { intent: string; trigger: { type: string; source: string }; actions: WorkflowAction[]; conditions: Array<{ if: string; then: string }>; variables: Record<string, unknown>; }
export interface Workflow { id: string; intent: string; status: WorkflowStatus; sequence_id: string; created_at: string; updated_at: string; }
export interface ObservedEvent { source: string; action: string; target: string; payload?: string; session_id: string; }
