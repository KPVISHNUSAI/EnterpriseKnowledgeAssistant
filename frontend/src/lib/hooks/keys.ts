/** TanStack Query keys. Prefix keys (e.g. `documents`) invalidate every query below them. */
export const keys = {
  me: ["me"] as const,
  dashboard: ["dashboard"] as const,
  workspaces: ["workspaces"] as const,
  workspace: (id: string) => ["workspaces", id] as const,
  workspaceMembers: (id: string) => ["workspaces", id, "members"] as const,
  documents: ["documents"] as const,
  documentList: (params: object) => ["documents", "list", params] as const,
  document: (id: string) => ["documents", "detail", id] as const,
  // indexedAt: a re-index replaces the chunks, so a new stamp means new data.
  chunks: (id: string, indexedAt: string | null) => ["documents", "chunks", id, indexedAt] as const,
  orgMembers: (params: object) => ["org-members", params] as const,
  invitations: (params: object) => ["invitations", params] as const,
  conversations: ["conversations"] as const,
  messages: (conversationId: string) => ["conversations", conversationId, "messages"] as const,
  usage: (days: number) => ["admin", "usage", days] as const,
  audit: (params: object) => ["admin", "audit", params] as const,
  proof: ["admin", "proof"] as const,
  proofRuns: (params: object) => ["admin", "proof", "runs", params] as const,
  proofRun: (id: string) => ["admin", "proof", "run", id] as const,
  search: (params: object) => ["search", params] as const,
};
