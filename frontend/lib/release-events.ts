export type Translator = (key: string, values?: Record<string, string | number>) => string;

export function formatReleaseEventSource(source: string, t: Translator) {
  if (source === "orchestrator") return t("Release orchestrator");
  if (source === "task") return t("Task worker");
  if (source === "coordinator") return t("Release coordinator");

  const agent = source.match(/^agent:(.+)$/);
  return agent ? t("Node agent: {node}", { node: agent[1] }) : source;
}

// Release events are intentionally persisted as canonical operational text by
// the API. Format the known event shapes at the presentation boundary so a
// locale switch does not need to change historical records.
export function formatReleaseEventMessage(message: string, t: Translator) {
  const created = message.match(/^Release (\S+) created for (\d+) node\(s\)\.$/);
  if (created) {
    return t("Release {releaseId} created for {count} nodes.", {
      releaseId: created[1],
      count: created[2],
    });
  }

  const taskEntered = message.match(/^Task (\S+) entered (\S+) state\.$/);
  if (taskEntered) {
    return t("Task {taskId} entered {status} state.", {
      taskId: taskEntered[1],
      status: t(taskEntered[2]),
    });
  }

  const acknowledgement = message.match(/^Node ACK received: ([^;]+); sing-box=(pass|fail), nftables=(pass|fail), health=(pass|fail)\.$/);
  if (acknowledgement) {
    return t("Node acknowledgement received: {stage}; sing-box={singbox}, nftables={nftables}, health={health}.", {
      stage: t(acknowledgement[1]),
      singbox: t(acknowledgement[2]),
      nftables: t(acknowledgement[3]),
      health: t(acknowledgement[4]),
    });
  }

  const completed = message.match(/^Release completed with status (\S+)\.$/);
  if (completed) {
    return t("Release completed with status {status}.", { status: t(completed[1]) });
  }

  return message;
}
