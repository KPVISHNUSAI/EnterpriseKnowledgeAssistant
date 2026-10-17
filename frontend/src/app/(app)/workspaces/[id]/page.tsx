import type { Metadata } from "next";
import { WorkspaceView } from "@/components/workspaces/workspace-view";

export const metadata: Metadata = { title: "Workspace" };

export default async function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <WorkspaceView id={id} />;
}
