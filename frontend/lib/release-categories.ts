const categoryLabels: Record<string, string> = {
  access_rules: "Access rules",
  routing_rules: "Routing rules",
  outbound_service: "Outbound service",
  subscription: "Subscription",
};

export function releaseCategoryLabels(categories?: string[]): string[] {
  const labels = [...new Set(categories || [])].map((category) => categoryLabels[category] || "Other changes");
  return labels.length ? [...new Set(labels)] : ["Other changes"];
}
