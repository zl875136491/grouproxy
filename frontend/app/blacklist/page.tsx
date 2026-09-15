"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CirclePlus, Trash2 } from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { createSourceBlacklist, deleteSourceBlacklist, getNodes, getSourceBlacklist, type SourceBlacklist } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { PageHeader } from "../../components/page-header";
import { SessionGate, useManagementSession } from "../../components/session-gate";
import { Button, ConfirmDialog, DetailDialog, Panel, StatusBadge } from "../../components/ui";

export default function BlacklistPage() {
  const { t, formatDate, formatNumber } = usePreferences();
  const searchParams = useSearchParams();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [nodeIds, setNodeIds] = useState<string[]>([]);
  const [direction, setDirection] = useState<SourceBlacklist["direction"]>("source");
  const [kind, setKind] = useState<SourceBlacklist["kind"]>("domain");
  const [pattern, setPattern] = useState("");
  const [comment, setComment] = useState("");
  const [removeTarget, setRemoveTarget] = useState<SourceBlacklist | null>(null);
  const initializedFromSearch = useRef(false);
  const blacklist = useQuery({ queryKey: ["source-blacklist"], queryFn: getSourceBlacklist, enabled: session === true });
  const nodes = useQuery({ queryKey: ["nodes"], queryFn: getNodes, enabled: session === true, staleTime: 30_000 });
  useEffect(() => {
    if (initializedFromSearch.current || !nodes.data) return;
    initializedFromSearch.current = true;
    const requestedNode = searchParams.get("node");
    if (requestedNode && nodes.data.some((node) => node.agent_id === requestedNode || node.id === requestedNode)) {
      const match = nodes.data.find((node) => node.agent_id === requestedNode || node.id === requestedNode);
      if (match) setNodeIds([match.agent_id]);
    }
  }, [searchParams, nodes.data]);
  const create = useMutation({
    mutationFn: () => createSourceBlacklist({
      node_ids: nodeIds,
      direction,
      kind,
      pattern: pattern.trim(),
      comment: comment.trim(),
      enabled: true,
    }),
    onSuccess: async (mutation) => {
      const created = mutation.rules?.length ? mutation.rules : [mutation.rule];
      queryClient.setQueryData<SourceBlacklist[]>(["source-blacklist"], (current = []) => [
        ...current.filter((entry) => !created.some((rule) => rule.id === entry.id)),
        ...created,
      ]);
      setPattern("");
      setComment("");
      setDirection("source");
      setKind("domain");
      setShowForm(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["source-blacklist"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
      ]);
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteSourceBlacklist(removeTarget!.id),
    onSuccess: async (mutation) => {
      queryClient.setQueryData<SourceBlacklist[]>(["source-blacklist"], (current = []) => current.filter((entry) => entry.id !== mutation.rule.id));
      setRemoveTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["source-blacklist"] }),
        queryClient.invalidateQueries({ queryKey: ["nodes"] }),
      ]);
    },
  });
  const nodeItems = nodes.data || [];
  const nodeNames = useMemo(() => new Map(nodeItems.map((node) => [node.agent_id, node.name])), [nodeItems]);
  function toggleNode(agentId: string) {
    setNodeIds((current) => current.includes(agentId) ? current.filter((id) => id !== agentId) : [...current, agentId]);
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pattern.trim() && nodeIds.length) create.mutate();
  }
  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (blacklist.isLoading || nodes.isLoading) return <LoadingState rows={6} />;
  if (blacklist.isError || nodes.isError) {
    const issue = blacklist.error || nodes.error;
    return <ErrorState error={issue instanceof Error ? issue.message : t("Unable to load blacklist.")} onRetry={() => void Promise.all([blacklist.refetch(), nodes.refetch()])} />;
  }
  const entries = blacklist.data || [];
  const placeholder = kind === "domain" ? "example.com" : kind === "ip" ? "203.0.113.10" : "203.0.113.0/24";
  return (
    <div className="page-stack page-fill list-page">
      <PageHeader
        eyebrow="POLICY"
        title={t("Blacklist")}
        description={t("Requests are allowed by default. Add an IP, CIDR, or domain rule per node only when it should be blocked.")}
        actions={<Button variant="primary" onClick={() => { create.reset(); setShowForm(true); }}><CirclePlus size={16} /> {t("Add blacklist rule")}</Button>}
      />
      <Panel className="list-panel">
        <div className="table-toolbar">
          <div className="toolbar-title"><Ban size={18} /><span>{t("{count} blacklist rules", { count: entries.length })}</span></div>
          <span className="toolbar-note">{t("Rules are delivered only to the selected nodes.")}</span>
        </div>
        <div className="table-wrap table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t("Node")}</th>
                <th>{t("Direction")}</th>
                <th>{t("Type")}</th>
                <th>{t("Pattern")}</th>
                <th>{t("Comment")}</th>
                <th>{t("Created")}</th>
                <th>{t("State")}</th>
                <th aria-label={t("Actions")} />
              </tr>
            </thead>
            <tbody>
              {entries.length ? entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{nodeNames.get(entry.node_id) || entry.node_id}</td>
                  <td><span className="type-tag">{t(entry.direction)}</span></td>
                  <td><span className="type-tag">{t(entry.kind)}</span></td>
                  <td className="mono">{entry.pattern}</td>
                  <td>{entry.comment || "-"}</td>
                  <td>{formatDate(entry.created_at)}</td>
                  <td><StatusBadge status={entry.enabled ? "enabled" : "disabled"} /></td>
                  <td>
                    <button className="row-icon-button row-icon-danger" aria-label={t("Delete {value}", { value: entry.pattern })} title={t("Delete blacklist rule")} onClick={() => setRemoveTarget(entry)}>
                      <Trash2 size={16} />
                    </button>
                  </td>
                </tr>
              )) : (
                <tr>
                  <td colSpan={8}>
                    <div className="table-empty"><Ban size={18} /> {t("No blacklist rules are blocking requests.")}</div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
      <DetailDialog
        open={showForm}
        onOpenChange={(open) => { if (!open && !create.isPending) setShowForm(false); }}
        title={t("Add blacklist rule")}
        description={t("Block a source or destination IP, CIDR, or domain on the selected nodes.")}
        contentClassName="policy-form-dialog"
      >
        <form className="policy-dialog-form" onSubmit={submit}>
          <div className="blacklist-node-picker">
            <div className="subscription-selection-heading">
              <div>
                <span className="panel-kicker">{t("TARGETS")}</span>
                <strong>{t("Nodes")}</strong>
              </div>
              <span>{t("{count} selected", { count: formatNumber(nodeIds.length) })}</span>
            </div>
            <div className="subscription-site-checks publish-site-checks blacklist-node-checks" role="group" aria-label={t("Nodes")}>
              {nodeItems.map((node) => {
                const selected = nodeIds.includes(node.agent_id);
                return (
                  <label className={selected ? "subscription-site-selected" : ""} key={node.id}>
                    <input className="site-radio-input" type="checkbox" checked={selected} onChange={() => toggleNode(node.agent_id)} />
                    <span className="site-choice-copy">
                      <strong>{node.name}</strong>
                      <small className="mono">{node.agent_id}</small>
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
          <label>
            <span>{t("Direction")}</span>
            <select value={direction} onChange={(event) => setDirection(event.target.value as SourceBlacklist["direction"])}>
              <option value="source">{t("source")}</option>
              <option value="destination">{t("destination")}</option>
            </select>
          </label>
          <label>
            <span>{t("Type")}</span>
            <select value={kind} onChange={(event) => setKind(event.target.value as SourceBlacklist["kind"])}>
              <option value="domain">{t("Domain")}</option>
              <option value="ip">{t("IP address")}</option>
              <option value="cidr">{t("CIDR")}</option>
            </select>
          </label>
          <label>
            <span>{t("Pattern")}</span>
            <input autoFocus value={pattern} onChange={(event) => setPattern(event.target.value)} placeholder={placeholder} />
          </label>
          <label>
            <span>{t("Comment")}</span>
            <input value={comment} onChange={(event) => setComment(event.target.value)} placeholder={t("Reason for block")} />
          </label>
          <div className="form-actions">
            <Button type="button" onClick={() => setShowForm(false)} disabled={create.isPending}>{t("Cancel")}</Button>
            <Button variant="primary" type="submit" disabled={create.isPending || !pattern.trim() || !nodeIds.length}>
              {create.isPending ? t("Adding...") : t("Add blacklist rule")}
            </Button>
          </div>
        </form>
      </DetailDialog>
      <ConfirmDialog
        open={Boolean(removeTarget)}
        onOpenChange={(open) => !open && setRemoveTarget(null)}
        title={t("Remove blacklist rule")}
        description={t("Remove {value} from the blacklist. The change is released automatically.", { value: removeTarget?.pattern || t("this pattern") })}
        confirmLabel={t("Remove blacklist rule")}
        danger
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
