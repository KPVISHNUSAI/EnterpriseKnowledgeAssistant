import type { Metadata } from "next";
import { DocumentView } from "@/components/documents/document-view";

export const metadata: Metadata = { title: "Document" };

export default async function DocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DocumentView id={id} />;
}
