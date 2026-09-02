"use client";

import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { ServerCog } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import type { Site } from "../lib/api";
import styles from "./control-plane-topology.module.css";

type Direction = "horizontal" | "vertical";
type Tone = "healthy" | "warning" | "danger" | "neutral";
type Translator = (key: string, values?: Record<string, string | number>) => string;

export type TopologySite = {
  site: Site;
  state: string;
  nodeCount: number;
};

type ControlPlaneNode = Node<{
  direction: Direction;
  title: string;
  eyebrow: string;
  detail: string;
  status: string;
}, "controlPlane">;

type EdgeSiteNode = Node<{
  direction: Direction;
  href: string;
  name: string;
  detail: string;
  status: string;
  tone: Tone;
}, "edgeSite">;

type TopologyNode = ControlPlaneNode | EdgeSiteNode;

function useCompactTopology() {
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 620px)");
    const update = () => setCompact(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return compact;
}

function toneForState(state: string): Tone {
  if (state === "healthy") return "healthy";
  if (state === "degraded") return "warning";
  if (state === "attention" || state === "shutdown") return "danger";
  return "neutral";
}

function toneClass(tone: Tone) {
  if (tone === "healthy") return styles.toneHealthy;
  if (tone === "warning") return styles.toneWarning;
  if (tone === "danger") return styles.toneDanger;
  return "";
}

function statusClass(tone: Tone) {
  if (tone === "healthy") return styles.statusHealthy;
  if (tone === "warning") return styles.statusWarning;
  if (tone === "danger") return styles.statusDanger;
  return "";
}

function edgeColor(tone: Tone) {
  if (tone === "healthy") return "var(--gp-green-dark)";
  if (tone === "warning") return "var(--gp-warning)";
  if (tone === "danger") return "var(--gp-danger)";
  return "var(--gp-line-strong)";
}

function ControlPlaneNodeView({ data }: NodeProps<ControlPlaneNode>) {
  const sourcePosition = data.direction === "vertical" ? Position.Bottom : Position.Right;
  return (
    <div className={`${styles.node} ${styles.controlNode}`} data-testid="control-plane-node">
      <Handle className={styles.handle} type="source" position={sourcePosition} isConnectable={false} />
      <span className={styles.controlIcon} aria-hidden="true"><ServerCog size={18} /></span>
      <div className={styles.nodeCopy}>
        <span className={styles.nodeEyebrow}>{data.eyebrow}</span>
        <strong>{data.title}</strong>
        <span className={styles.nodeDetail}>{data.detail}</span>
      </div>
      <span className={`${styles.statusPill} ${styles.controlStatus}`}>{data.status}</span>
    </div>
  );
}

function EdgeSiteNodeView({ data }: NodeProps<EdgeSiteNode>) {
  const router = useRouter();
  const targetPosition = data.direction === "vertical" ? Position.Top : Position.Left;
  return (
    <div className={`${styles.node} ${styles.siteNode}`} data-testid="edge-site-node">
      <Handle className={styles.handle} type="target" position={targetPosition} isConnectable={false} />
      <Link
        className={`${styles.siteLink} nodrag nopan`}
        href={data.href}
        aria-label={data.name}
        onPointerDown={(event) => event.stopPropagation()}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          router.push(data.href);
        }}
      >
        <span className={`${styles.stateDot} ${toneClass(data.tone)}`} aria-hidden="true" />
        <span className={styles.nodeCopy}>
          <strong>{data.name}</strong>
          <span className={styles.nodeDetail}>{data.detail}</span>
        </span>
        <span className={`${styles.statusPill} ${statusClass(data.tone)}`}>{data.status}</span>
      </Link>
    </div>
  );
}

const nodeTypes = {
  controlPlane: ControlPlaneNodeView,
  edgeSite: EdgeSiteNodeView,
};

export function ControlPlaneTopology({
  sites,
  onlineNodes,
  totalNodes,
  formatNumber,
  t,
}: {
  sites: TopologySite[];
  onlineNodes: number;
  totalNodes: number;
  formatNumber: (value: number) => string;
  t: Translator;
}) {
  const compact = useCompactTopology();
  const direction: Direction = compact ? "vertical" : "horizontal";
  const { edges, flowNodes, height } = useMemo(() => {
    const siteStep = compact ? 92 : 94;
    const firstSiteY = compact ? 124 : 12;
    const finalSiteY = firstSiteY + Math.max(0, sites.length - 1) * siteStep;
    const controlY = compact ? 12 : Math.max(12, finalSiteY / 2);
    const controlPosition = compact ? { x: 54, y: controlY } : { x: 18, y: controlY };
    const siteX = compact ? 54 : 288;
    const controlWidth = compact ? 228 : 198;
    const siteWidth = compact ? 228 : 238;
    const flowHeight = compact
      ? Math.max(430, finalSiteY + 94)
      : Math.max(360, finalSiteY + 94);
    const siteNodes: EdgeSiteNode[] = sites.map(({ site, state, nodeCount }, index) => {
      const tone = toneForState(state);
      const detail = nodeCount
        ? `${formatNumber(nodeCount)} ${t(nodeCount === 1 ? "node" : "nodes")}`
        : t("No node enrolled");
      return {
        id: `site-${site.id}`,
        type: "edgeSite",
        position: { x: siteX, y: firstSiteY + index * siteStep },
        targetPosition: direction === "vertical" ? Position.Top : Position.Left,
        data: {
          direction,
          href: `/sites/${site.slug}/cidrs`,
          name: t(site.name),
          detail,
          status: t(state),
          tone,
        },
        style: { width: siteWidth },
      };
    });
    const controlNode: ControlPlaneNode = {
      id: "control-plane",
      type: "controlPlane",
      position: controlPosition,
      sourcePosition: direction === "vertical" ? Position.Bottom : Position.Right,
      data: {
        direction,
        eyebrow: t("CONTROL PLANE"),
        title: t("Control plane"),
        detail: `${formatNumber(onlineNodes)} / ${formatNumber(totalNodes)} ${t("Nodes online")}`,
        status: t("online"),
      },
      style: { width: controlWidth },
    };
    const topologyEdges: Edge[] = siteNodes.map((siteNode) => {
      const tone = siteNode.data.tone;
      const color = edgeColor(tone);
      return {
        id: `control-plane-${siteNode.id}`,
        source: controlNode.id,
        target: siteNode.id,
        type: "smoothstep",
        animated: tone === "healthy" || tone === "warning",
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
        style: { stroke: color, strokeWidth: 1.5 },
      };
    });
    return { flowNodes: [controlNode, ...siteNodes] as TopologyNode[], edges: topologyEdges, height: flowHeight };
  }, [compact, direction, formatNumber, onlineNodes, sites, t, totalNodes]);

  return (
    <div className={styles.flow} style={{ height }} aria-label={t("Control plane to edge sites")}>
      <ReactFlow<TopologyNode, Edge>
        key={`${direction}-${sites.length}`}
        nodes={flowNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: compact ? 0.13 : 0.18, maxZoom: 1.1 }}
        minZoom={0.4}
        maxZoom={1.25}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesReconnectable={false}
        elementsSelectable
        zoomOnScroll={false}
        zoomOnDoubleClick={false}
        panOnDrag
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--gp-line)" gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
