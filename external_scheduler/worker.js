const REPOSITORY = "Evanlei2025/market-advisor";
const API = "https://api.github.com/repos/" + REPOSITORY;
const START_TIMEOUT_MS = 120_000;
const POLL_INTERVAL_MS = 10_000;

const CRONS = {
  "35 11 * * 1-5": { kind: "daily", workflow: "daily-report.yml", slot: "19:35:00" },
  "35 9 * * 6": { kind: "weekly", workflow: "daily-report.yml", slot: "17:35:00" },
  "17 14 * * *": { kind: "heartbeat", workflow: "public-heartbeat.yml", slot: "22:17:00" },
};

function shanghaiParts(timestamp) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(timestamp));
  return Object.fromEntries(parts.filter(({ type }) => type !== "literal").map(({ type, value }) => [type, value]));
}

export function taskForCron(cron, scheduledTime) {
  const definition = CRONS[cron];
  if (!definition) throw new Error("unrecognized external scheduler cron");
  const parts = shanghaiParts(scheduledTime);
  const logicalDate = `${parts.year}-${parts.month}-${parts.day}`;
  const utcId = new Date(scheduledTime).toISOString().replace(/\.\d{3}Z$/, "Z").replace(/[-:]/g, "");
  const triggerId = `cf-${definition.kind}-${utcId}`;
  const scheduledFor = `${logicalDate}T${definition.slot}+08:00`;
  const inputs = definition.kind === "heartbeat"
    ? { logical_date: logicalDate, scheduled_for: scheduledFor, trigger_id: triggerId }
    : { publish_weekly: definition.kind === "weekly", logical_date: logicalDate,
        backfill: false, scheduled_for: scheduledFor, trigger_id: triggerId };
  return { ...definition, logicalDate, scheduledFor, triggerId, inputs };
}

async function api(env, path, init = {}, request = fetch) {
  const response = await request(API + path, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "market-advisor-external-scheduler",
      ...init.headers,
    },
  });
  if (!response.ok) throw new Error(`GitHub API HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}

async function findRun(env, task, request) {
  const query = new URLSearchParams({ branch: "master", event: "workflow_dispatch", per_page: "100" });
  const result = await api(env, `/actions/workflows/${task.workflow}/runs?${query}`, {}, request);
  return result.workflow_runs.find((run) => run.display_title?.endsWith(task.triggerId)) || null;
}

const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

export async function ensureStarted(task, env, { request = fetch, wait = pause, timeoutMs = START_TIMEOUT_MS,
  pollIntervalMs = POLL_INTERVAL_MS } = {}) {
  let run = await findRun(env, task, request);
  if (!run) {
    await api(env, `/actions/workflows/${task.workflow}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: "master", inputs: task.inputs }),
    }, request);
  }

  const startedBy = Date.now() + timeoutMs;
  do {
    run = await findRun(env, task, request);
    if (run?.run_started_at && (run.status === "in_progress" || run.status === "completed")) return run;
    if (Date.now() >= startedBy) break;
    await wait(Math.min(pollIntervalMs, Math.max(1, startedBy - Date.now())));
  } while (true);
  const status = run ? `status=${run.status}` : "run not found";
  throw new Error(`GitHub workflow did not start within ${timeoutMs / 1000}s (${status})`);
}

async function notifyFailure(task, error, env, run = null) {
  console.error(JSON.stringify({ event: "workflow_start_failed", task: task.kind,
    logical_date: task.logicalDate, scheduled_for: task.scheduledFor,
    trigger_id: task.triggerId, run_id: run?.id || null, run_url: run?.html_url || null,
    error: error.message }));
  if (!env.SC_KEY) throw error;
  const title = "市场简报外部调度未确认工作流启动";
  const details = [
    `task: ${task.kind}`,
    `logical run date: ${task.logicalDate}`,
    `scheduled slot: ${task.scheduledFor}`,
    `trigger id: ${task.triggerId}`,
    `run ID: ${run?.id || "not found"}`,
    `run: ${run?.html_url || "not available"}`,
    `error: ${error.message}`,
  ].join("\n\n");
  const response = await fetch(`https://sctapi.ftqq.com/${env.SC_KEY}.send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, desp: details }),
  });
  const result = await response.json();
  if (!response.ok || result.code !== 0) throw new Error("operator alert delivery failed after scheduler error");
  throw error;
}

export async function handleScheduled(event, env, options = {}) {
  const task = taskForCron(event.cron, event.scheduledTime);
  try {
    const run = await ensureStarted(task, env, options);
    console.log(JSON.stringify({ event: "workflow_started", task: task.kind,
      logical_date: task.logicalDate, scheduled_for: task.scheduledFor,
      delay_seconds: Math.max(0, Math.floor((Date.now() - event.scheduledTime) / 1000)),
      trigger_id: task.triggerId, run_id: run.id, run_url: run.html_url, status: run.status }));
  } catch (error) {
    await notifyFailure(task, error, env);
  }
}

export default {
  scheduled(event, env, context) {
    context.waitUntil(handleScheduled(event, env));
  },
};
