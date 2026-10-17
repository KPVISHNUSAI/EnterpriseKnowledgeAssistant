import type { Metadata } from "next";
import { ChatWorkspace } from "@/components/chat/chat-workspace";

export const metadata: Metadata = { title: "Chat" };

// The workspace lives in the layout so streaming survives /chat -> /chat/<new id>.
export default function ChatLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <ChatWorkspace />
      {children}
    </>
  );
}
