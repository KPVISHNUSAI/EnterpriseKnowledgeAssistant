"use client";

import { ShieldAlert } from "lucide-react";
import { AuditLog } from "@/components/admin/audit-log";
import { Invitations } from "@/components/admin/invitations";
import { OrgMembers } from "@/components/admin/org-members";
import { PermissionProof } from "@/components/admin/permission-proof";
import { UsageView } from "@/components/admin/usage-view";
import { useSession } from "@/components/auth/session";
import { LinkButton } from "@/components/ui/button";
import { EmptyState, PageHeader } from "@/components/ui/feedback";
import { Tab, TabList, TabPanel, Tabs } from "@/components/ui/tabs";
import { useUrlState } from "@/lib/hooks/url-state";

export default function AdminPage() {
  const { capabilities } = useSession();
  const { params, update } = useUrlState();

  const tabs = [
    ...(capabilities.can_manage_organization ? [{ id: "usage", label: "Usage" }] : []),
    ...(capabilities.can_view_audit_log ? [{ id: "audit", label: "Audit log" }] : []),
    ...(capabilities.can_manage_organization
      ? [
          { id: "members", label: "Members" },
          { id: "invitations", label: "Invitations" },
          { id: "proof", label: "Permission proof" },
        ]
      : []),
  ];

  const [first] = tabs;
  if (!first) {
    return (
      <EmptyState
        icon={<ShieldAlert />}
        title="Administrators only"
        description="You do not have permission to view this page. Ask an organization admin if you need access."
        action={<LinkButton href="/dashboard">Back to dashboard</LinkButton>}
      />
    );
  }

  const requested = params.get("tab");
  const active = tabs.find((tab) => tab.id === requested)?.id ?? first.id;

  return (
    <>
      <PageHeader
        title="Administration"
        description="Usage, audit trail, members, invitations and the permission leak test for your organization."
      />
      <Tabs value={active} onValueChange={(tab) => update({ tab, run: null })}>
        <TabList aria-label="Administration sections" className="print:hidden">
          {tabs.map((tab) => (
            <Tab key={tab.id} value={tab.id} data-testid={`admin-tab-${tab.id}`}>
              {tab.label}
            </Tab>
          ))}
        </TabList>
        {capabilities.can_manage_organization ? (
          <TabPanel value="usage">
            <UsageView />
          </TabPanel>
        ) : null}
        {capabilities.can_view_audit_log ? (
          <TabPanel value="audit">
            <AuditLog />
          </TabPanel>
        ) : null}
        {capabilities.can_manage_organization ? (
          <>
            <TabPanel value="members">
              <OrgMembers />
            </TabPanel>
            <TabPanel value="invitations">
              <Invitations />
            </TabPanel>
            <TabPanel value="proof">
              <PermissionProof />
            </TabPanel>
          </>
        ) : null}
      </Tabs>
    </>
  );
}
