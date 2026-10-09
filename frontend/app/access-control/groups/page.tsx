"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CirclePlus, Layers, Pencil, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "../../../components/data-state";
import { FilterSearch, FilterSelect } from "../../../components/list-filters";
import { PageHeader } from "../../../components/page-header";
import { SessionGate, useManagementSession } from "../../../components/session-gate";
import { toastErrorMessage, useToast } from "../../../components/toast";
import { Button, ConfirmDialog, DetailDialog, IconButton, Panel } from "../../../components/ui";
import { createNetworkGroup, deleteNetworkGroup, getNetworkGroups, updateNetworkGroup, type NetworkGroup, type NetworkGroupEntry } from "../../../lib/api";
import { usePreferences } from "../../../lib/preferences";
import { ACCESS_PAGE_SIZE, ListPagination, useDebouncedValue } from "../list-controls";

type DraftEntry = NetworkGroupEntry & { key: string };

let entryKey = 0;

function nextEntryKey() {
  entryKey += 1;
  return `entry-${entryKey}`;
}

function draftEntries(group: NetworkGroup | null): DraftEntry[] {
  const source = group?.entries.length ? group.entries : [{ kind: "domain" as const, pattern: "" }];
  return source.map((entry) => ({ ...entry, key: nextEntryKey() }));
}

function kindLabel(kind: NetworkGroupEntry["kind"], t: (key: string) => string) {
  if (kind === "ip") return t("IP");
  if (kind === "cidr") return t("CIDR");
  return t("domain");
}

export default function AccessGroupsPage() {
  const { t, formatDate, formatNumber } = usePreferences();
  const session = useManagementSession();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [editor, setEditor] = useState<NetworkGroup | "new" | null>(null);
  const [pendingDelete, setPendingDelete] = useState<NetworkGroup | null>(null);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const deferredSearch = useDebouncedValue(search.trim());
  const filterKey = `${deferredSearch}|${kind}`;
  const [pageState, setPageState] = useState({ filterKey, offset: 0 });
  if (pageState.filterKey !== filterKey) setPageState({ filterKey, offset: 0 });
  const offset = pageState.offset;
  const setOffset = (update: number | ((current: number) => number)) => {
    setPageState((current) => ({ ...current, offset: typeof update === "function" ? update(current.offset) : update }));
  };
  const groups = useQuery({
    queryKey: ["network-groups", deferredSearch, kind, offset],
    queryFn: () => getNetworkGroups({
      search: deferredSearch || undefined,
      kind: kind ? kind as NetworkGroupEntry["kind"] : undefined,
      limit: ACCESS_PAGE_SIZE,
      offset,
    }),
    enabled: session === true,
  });
  const removeGroup = useMutation({
    mutationFn: (groupId: string) => deleteNetworkGroup(groupId),
    onSuccess: async () => {
      setPendingDelete(null);
      await queryClient.invalidateQueries({ queryKey: ["network-groups"] });
    },
    onError: (error) => {
      toast({ title: t("Operation failed"), description: t(toastErrorMessage(error)), variant: "destructive" });
    },
  });

  if (session === null) return <LoadingState rows={6} />;
  if (!session) return <SessionGate />;
  if (groups.isLoading) return <LoadingState rows={8} />;
  if (groups.isError || !groups.data) return <ErrorState error={t("Unable to load access control.")} onRetry={() => void groups.refetch()} />;

  const page = groups.data;
  const filtering = Boolean(deferredSearch || kind);
  return (
    <div className="page-stack page-fill list-page">
      <PageHeader
        eyebrow="POLICY"
        title="Access groups"
        description="Named collections of IP addresses, CIDR ranges, and domains."
        actions={<Button variant="primary" onClick={() => setEditor("new")}><CirclePlus size={16} /> {t("New access group")}</Button>}
      />
      <Panel className="list-panel">
        <div className="table-toolbar">
          <div className="toolbar-title"><Layers size={18} /><span>{t("{count} access groups", { count: formatNumber(page.total) })}</span></div>
        </div>
        <div className="list-filters" role="search">
          <FilterSearch value={search} setValue={setSearch} placeholder="Search access groups" />
          <FilterSelect
            label="Entry type"
            value={kind}
            setValue={setKind}
            options={[
              { value: "", label: "All types" },
              { value: "domain", label: "domain" },
              { value: "ip", label: "IP" },
              { value: "cidr", label: "CIDR" },
            ]}
          />
        </div>
        {page.items.length ? (
          <div className="table-wrap table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{t("Name")}</th>
                  <th>{t("Network entries")}</th>
                  <th>{t("Updated")}</th>
                  <th aria-label={t("Actions")} />
                </tr>
              </thead>
              <tbody>
                {page.items.map((group) => (
                  <tr key={group.id}>
                    <td><strong>{group.name}</strong><span className="cell-secondary mono">{group.id}</span></td>
                    <td>{group.entries.map((entry) => <div key={`${entry.kind}-${entry.pattern}`}><span className="mono">{kindLabel(entry.kind, t)}</span> {entry.pattern}</div>)}</td>
                    <td>{formatDate(group.updated_at)}</td>
                    <td>
                      <div className="row-actions">
                        <IconButton label={t("Edit {name}", { name: group.name })} onClick={() => setEditor(group)}><Pencil size={15} /></IconButton>
                        <IconButton label={t("Delete network group")} className="row-icon-danger" onClick={() => setPendingDelete(group)}><Trash2 size={15} /></IconButton>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={filtering ? "No matching access groups" : "No access groups"}
            detail={filtering ? "No access groups match the current filters." : "Create a named collection of IP, CIDR, and domain values."}
          />
        )}
        <ListPagination
          offset={page.offset}
          pageCount={page.items.length}
          total={page.total}
          fetching={groups.isFetching}
          onPrevious={() => setOffset((current) => Math.max(0, current - ACCESS_PAGE_SIZE))}
          onNext={() => setOffset((current) => current + ACCESS_PAGE_SIZE)}
        />
      </Panel>
      <DetailDialog
        open={editor !== null}
        onOpenChange={(open) => { if (!open) setEditor(null); }}
        title={editor && editor !== "new" ? "Edit access group" : "New access group"}
        description={editor && editor !== "new" ? "Change the access group name and its network entries." : undefined}
        contentClassName="policy-form-dialog"
      >
        {editor ? <GroupForm key={editor === "new" ? "new" : editor.id} group={editor === "new" ? null : editor} onDone={() => setEditor(null)} /> : null}
      </DetailDialog>
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => { if (!open && !removeGroup.isPending) setPendingDelete(null); }}
        title="Delete network group"
        description={pendingDelete ? t("Delete access group {name}? Rules that still use it must be removed first.", { name: pendingDelete.name }) : ""}
        confirmLabel="Delete network group"
        danger
        busy={removeGroup.isPending}
        onConfirm={() => pendingDelete && removeGroup.mutate(pendingDelete.id)}
      />
    </div>
  );
}

function GroupForm({ group, onDone }: { group: NetworkGroup | null; onDone: () => void }) {
  const { t } = usePreferences();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [name, setName] = useState(group?.name || "");
  const [entries, setEntries] = useState<DraftEntry[]>(() => draftEntries(group));
  const save = useMutation({
    mutationFn: () => {
      const payload = { name: name.trim(), entries: entries.map(({ kind, pattern }) => ({ kind, pattern: pattern.trim() })) };
      return group ? updateNetworkGroup(group.id, payload) : createNetworkGroup(payload);
    },
    onSuccess: async () => {
      onDone();
      await queryClient.invalidateQueries({ queryKey: ["network-groups"] });
    },
    onError: (error) => {
      toast({ title: t("Operation failed"), description: t(toastErrorMessage(error)), variant: "destructive" });
    },
  });
  const complete = name.trim().length > 0 && entries.length > 0 && entries.every((entry) => entry.pattern.trim());
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (complete && !save.isPending) save.mutate();
  };
  return (
    <form className="policy-dialog-form" onSubmit={submit}>
      <label>
        <span>{t("Name")}</span>
        <input autoFocus value={name} maxLength={128} onChange={(event) => setName(event.target.value)} />
      </label>
      <div className="access-entry-block">
        <span className="access-field-label">{t("Network entries")}</span>
        <div className="access-entry-list">
          {entries.map((entry) => (
            <div className="access-entry-row" key={entry.key}>
              <select value={entry.kind} aria-label={t("Entry type")} onChange={(event) => setEntries((current) => current.map((item) => item.key === entry.key ? { ...item, kind: event.target.value as NetworkGroupEntry["kind"] } : item))}>
                <option value="domain">{t("domain")}</option>
                <option value="ip">{t("IP")}</option>
                <option value="cidr">{t("CIDR")}</option>
              </select>
              <input value={entry.pattern} aria-label={t("Entry value")} onChange={(event) => setEntries((current) => current.map((item) => item.key === entry.key ? { ...item, pattern: event.target.value } : item))} />
              <IconButton label="Remove entry" onClick={() => setEntries((current) => current.filter((item) => item.key !== entry.key))}><Trash2 size={15} /></IconButton>
            </div>
          ))}
        </div>
        <div>
          <Button type="button" onClick={() => setEntries((current) => [...current, { key: nextEntryKey(), kind: "domain", pattern: "" }])}><CirclePlus size={15} /> {t("Add entry")}</Button>
        </div>
      </div>
      <div className="form-actions">
        <Button type="button" onClick={onDone} disabled={save.isPending}>{t("Cancel")}</Button>
        <Button variant="primary" type="submit" disabled={!complete || save.isPending}>{save.isPending ? t("Saving...") : t(group ? "Save changes" : "Create")}</Button>
      </div>
    </form>
  );
}
