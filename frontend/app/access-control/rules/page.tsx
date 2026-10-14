"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Pencil, ShieldCheck, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "../../../components/data-state";
import { FilterSearch, FilterSelect } from "../../../components/list-filters";
import { PageHeader } from "../../../components/page-header";
import { SessionGate, useManagementSession } from "../../../components/session-gate";
import { toastErrorMessage, useToast } from "../../../components/toast";
import { Button, ConfirmDialog, DetailDialog, IconButton, Panel } from "../../../components/ui";
import { createAccessControl, deleteAccessControl, getAccessControl, getNetworkGroups, getNodes, updateAccessControl, type AccessControlRule, type NetworkGroup, type Node } from "../../../lib/api";
import { usePreferences } from "../../../lib/preferences";
import { ACCESS_PAGE_SIZE, ListPagination, useDebouncedValue } from "../list-controls";

type RuleDraft = {
  id?: string;
  nodeIds: string[];
  groupIds: string[];
  groupNames: string[];
  mode: AccessControlRule["mode"];
  direction: AccessControlRule["direction"];
  reason: string;
};

function modeLabel(mode: AccessControlRule["mode"], t: (key: string) => string) {
  return t(mode === "allow" ? "Whitelist" : "Blacklist");
}

function directionLabel(direction: AccessControlRule["direction"], t: (key: string) => string) {
  return t(direction === "destination" ? "Destination group" : "source");
}

export default function AccessRulesPage() {
  const { t, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [editor, setEditor] = useState<RuleDraft | null>(null);
  const [pendingDelete, setPendingDelete] = useState<AccessControlRule | null>(null);
  const [search, setSearch] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [mode, setMode] = useState("");
  const [direction, setDirection] = useState("");
  const deferredSearch = useDebouncedValue(search.trim());
  const filterKey = `${deferredSearch}|${nodeId}|${groupId}|${mode}|${direction}`;
  const [pageState, setPageState] = useState({ filterKey, offset: 0 });
  if (pageState.filterKey !== filterKey) setPageState({ filterKey, offset: 0 });
  const offset = pageState.offset;
  const setOffset = (update: number | ((current: number) => number)) => {
    setPageState((current) => ({ ...current, offset: typeof update === "function" ? update(current.offset) : update }));
  };
  const rules = useQuery({
    queryKey: ["access-control", deferredSearch, nodeId, groupId, mode, direction, offset],
    queryFn: () => getAccessControl({
      search: deferredSearch || undefined,
      nodeId: nodeId || undefined,
      networkGroupId: groupId || undefined,
      mode: mode ? mode as AccessControlRule["mode"] : undefined,
      direction: direction ? direction as AccessControlRule["direction"] : undefined,
      limit: ACCESS_PAGE_SIZE,
      offset,
    }),
    enabled: session === true,
  });
  const groups = useQuery({
    queryKey: ["network-groups", "options"],
    queryFn: () => getNetworkGroups({ limit: 500 }),
    enabled: session === true,
  });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true });
  const removeRule = useMutation({
    mutationFn: (ruleId: string) => deleteAccessControl(ruleId),
    onSuccess: async () => {
      setPendingDelete(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["access-control"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
      ]);
    },
    onError: (error) => {
      toast({ title: t("Operation failed"), description: t(toastErrorMessage(error)), variant: "destructive" });
    },
  });

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (rules.isLoading || nodes.isLoading) return <LoadingState rows={8} />;
  if (rules.isError || !rules.data) return <ErrorState error={t("Unable to load access control.")} onRetry={() => void rules.refetch()} />;

  const page = rules.data;
  const nodeItems = nodes.data || [];
  const groupItems = groups.data?.items || [];
  const nodeName = (agentId: string) => nodeItems.find((node) => node.agent_id === agentId)?.name || agentId;
  const filtering = Boolean(deferredSearch || nodeId || groupId || mode || direction);
  const openEditor = (rule?: AccessControlRule) => {
    setEditor(rule ? {
      id: rule.id,
      nodeIds: rule.node_ids,
      groupIds: rule.network_group_ids,
      groupNames: rule.network_group_names,
      mode: rule.mode,
      direction: rule.direction,
      reason: rule.reason,
    } : { nodeIds: [], groupIds: [], groupNames: [], mode: "deny", direction: "source", reason: "" });
  };
  return (
    <div className="page-stack page-fill list-page">
      <PageHeader
        eyebrow="POLICY"
        title="Access rules"
        description="Bind an access group to selected nodes as a blacklist or whitelist."
        actions={<Button variant="primary" onClick={() => openEditor()}><ShieldCheck size={16} /> {t("New access rule")}</Button>}
      />
      <Panel className="list-panel">
        <div className="table-toolbar">
          <div className="toolbar-title"><Ban size={18} /><span>{t("{count} access rules", { count: formatNumber(page.total) })}</span></div>
        </div>
        <div className="list-filters" role="search">
          <FilterSearch value={search} setValue={setSearch} placeholder="Search access rules" />
          <FilterSelect label="Node" value={nodeId} setValue={setNodeId} options={[{ value: "", label: "All nodes" }, ...nodeItems.map((node) => ({ value: node.agent_id, label: node.name }))]} />
          <FilterSelect label="Access groups" value={groupId} setValue={setGroupId} options={[{ value: "", label: "All groups" }, ...groupItems.map((group) => ({ value: group.id, label: group.name }))]} />
          <FilterSelect label="Mode" value={mode} setValue={setMode} options={[{ value: "", label: "All modes" }, { value: "deny", label: "Blacklist" }, { value: "allow", label: "Whitelist" }]} />
          <FilterSelect label="Direction" value={direction} setValue={setDirection} options={[{ value: "", label: "All directions" }, { value: "source", label: "source" }, { value: "destination", label: "Destination group" }]} />
        </div>
        {page.items.length ? (
          <div className="table-wrap table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{t("Nodes")}</th>
                  <th>{t("Access groups")}</th>
                  <th>{t("Mode")}</th>
                  <th>{t("Direction")}</th>
                  <th>{t("Reason")}</th>
                  <th aria-label={t("Actions")} />
                </tr>
              </thead>
              <tbody>
                {page.items.map((rule) => (
                  <tr key={rule.id}>
                    <td>{rule.node_ids.map((nodeId) => <div key={nodeId}><strong>{nodeName(nodeId)}</strong><span className="cell-secondary mono">{nodeId}</span></div>)}</td>
                    <td>{rule.network_group_names.map((name, index) => <div key={rule.network_group_ids[index]}>{name}</div>)}</td>
                    <td>{modeLabel(rule.mode, t)}</td>
                    <td>{directionLabel(rule.direction, t)}</td>
                    <td>{rule.reason}</td>
                    <td>
                      <div className="row-actions">
                        <IconButton label={t("Edit {name}", { name: rule.network_group_names.join(", ") })} onClick={() => openEditor(rule)}><Pencil size={15} /></IconButton>
                        <IconButton label="Delete access rule" className="row-icon-danger" onClick={() => setPendingDelete(rule)}><Trash2 size={15} /></IconButton>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={filtering ? "No matching access rules" : "No access rules"}
            detail={filtering ? "No access rules match the current filters." : "Bind an access group to one or more nodes."}
          />
        )}
        <ListPagination
          offset={page.offset}
          pageCount={page.items.length}
          total={page.total}
          fetching={rules.isFetching}
          onPrevious={() => setOffset((current) => Math.max(0, current - ACCESS_PAGE_SIZE))}
          onNext={() => setOffset((current) => current + ACCESS_PAGE_SIZE)}
        />
      </Panel>
      <DetailDialog
        open={editor !== null}
        onOpenChange={(open) => { if (!open) setEditor(null); }}
        title={editor?.id ? "Edit access rule" : "New access rule"}
        description={editor?.id ? "Change the selected nodes, mode, direction, or reason. Clearing a node removes this rule from that node." : undefined}
        contentClassName="policy-form-dialog"
      >
        {editor ? <RuleForm key={editor.id || "new"} draft={editor} groups={groupItems} nodes={nodeItems} onDone={() => setEditor(null)} /> : null}
      </DetailDialog>
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => { if (!open && !removeRule.isPending) setPendingDelete(null); }}
        title="Delete access rule"
        description="Delete this access rule? The change is released to the node automatically."
        confirmLabel="Delete access rule"
        danger
        busy={removeRule.isPending}
        onConfirm={() => pendingDelete && removeRule.mutate(pendingDelete.id)}
      />
    </div>
  );
}

function RuleForm({
  draft,
  groups,
  nodes,
  onDone,
}: {
  draft: RuleDraft;
  groups: NetworkGroup[];
  nodes: Node[];
  onDone: () => void;
}) {
  const { t, formatNumber } = usePreferences();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [nodeIds, setNodeIds] = useState(draft.nodeIds);
  const [groupIds, setGroupIds] = useState(draft.groupIds);
  const [mode, setMode] = useState(draft.mode);
  const [direction, setDirection] = useState(draft.direction);
  const [reason, setReason] = useState(draft.reason);
  const save = useMutation({
    mutationFn: () => {
      const payload = { node_ids: nodeIds, network_group_ids: groupIds, mode, direction, reason: reason.trim(), enabled: true };
      return draft.id ? updateAccessControl(draft.id, payload) : createAccessControl(payload);
    },
    onSuccess: async () => {
      onDone();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["access-control"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
      ]);
    },
    onError: (error) => {
      toast({ title: t("Operation failed"), description: t(toastErrorMessage(error)), variant: "destructive" });
    },
  });
  const complete = Boolean(groupIds.length && nodeIds.length && reason.trim());
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (complete && !save.isPending) save.mutate();
  };
  const toggleNode = (agentId: string) => {
    setNodeIds((current) => current.includes(agentId) ? current.filter((item) => item !== agentId) : [...current, agentId]);
  };
  const groupChoices = [...groups, ...draft.groupIds.filter((id) => !groups.some((group) => group.id === id)).map((id) => ({ id, name: draft.groupNames[draft.groupIds.indexOf(id)], entries: [], created_by: "", created_at: "", updated_at: "" }))];
  return (
    <form className="policy-dialog-form" onSubmit={submit}>
      <label>
        <span>{t("Access groups")}</span>
        <div className="subscription-site-checks blacklist-node-checks" role="group" aria-label={t("Access groups")}>
          {groupChoices.map((group) => <label key={group.id} className={groupIds.includes(group.id) ? "subscription-site-selected" : undefined}>
            <input type="checkbox" checked={groupIds.includes(group.id)} onChange={() => setGroupIds((current) => current.includes(group.id) ? current.filter((id) => id !== group.id) : [...current, group.id])} />
            <span><strong>{group.name}</strong><small className="mono">{group.id}</small></span>
          </label>)}
        </div>
      </label>
      <div className="access-node-block">
        <div className="subscription-selection-heading">
          <strong className="access-field-label">{t("Select nodes")}</strong>
          <span>{t("{count} selected", { count: formatNumber(nodeIds.length) })}</span>
        </div>
        <div className="subscription-site-checks blacklist-node-checks" role="group" aria-label={t("Select nodes")}>
          {nodes.map((node) => {
            const selected = nodeIds.includes(node.agent_id);
            return (
              <label className={selected ? "subscription-site-selected" : undefined} key={node.id}>
                <input type="checkbox" checked={selected} onChange={() => toggleNode(node.agent_id)} />
                <span><strong>{node.name}</strong><small className="mono">{node.agent_id}</small></span>
              </label>
            );
          })}
        </div>
        <p className="policy-hint">{t("Choose one or more nodes. Nothing is selected by default.")}</p>
      </div>
      <div className="access-rule-pair">
        <label>
          <span>{t("Mode")}</span>
          <select value={mode} onChange={(event) => setMode(event.target.value as AccessControlRule["mode"])}>
            <option value="deny">{t("Blacklist")}</option>
            <option value="allow">{t("Whitelist")}</option>
          </select>
        </label>
        <label>
          <span>{t("Direction")}</span>
          <select value={direction} onChange={(event) => setDirection(event.target.value as AccessControlRule["direction"])}>
            <option value="source">{t("source")}</option>
            <option value="destination">{t("Destination group")}</option>
          </select>
        </label>
      </div>
      <p className="policy-hint">{t("A whitelist does not change the default allow-all policy. It only exempts traffic that also matches a blacklist.")}</p>
      <label>
        <span>{t("Reason")}</span>
        <input value={reason} maxLength={512} onChange={(event) => setReason(event.target.value)} />
      </label>
      <div className="form-actions">
        <Button type="button" onClick={onDone} disabled={save.isPending}>{t("Cancel")}</Button>
        <Button variant="primary" type="submit" disabled={!complete || save.isPending}>{save.isPending ? t("Saving...") : t(draft.id ? "Save changes" : "Create")}</Button>
      </div>
    </form>
  );
}
