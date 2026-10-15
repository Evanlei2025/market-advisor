import assert from "node:assert/strict";
import test from "node:test";
import { ensureStarted, taskForCron } from "./worker.js";

test("daily external trigger keeps its scheduled Shanghai date and slot", () => {
  const task = taskForCron("35 11 * * 1-5", Date.parse("2026-10-15T11:35:00Z"));
  assert.equal(task.kind, "daily");
  assert.equal(task.logicalDate, "2026-10-15");
  assert.equal(task.scheduledFor, "2026-10-15T19:35:00+08:00");
  assert.equal(task.inputs.publish_weekly, false);
  assert.equal(task.inputs.trigger_id, "cf-daily-20261015T113500Z");
});

test("Saturday external trigger requests the weekly publication", () => {
  const task = taskForCron("35 9 * * 6", Date.parse("2026-10-17T09:35:00Z"));
  assert.equal(task.kind, "weekly");
  assert.equal(task.logicalDate, "2026-10-17");
  assert.equal(task.inputs.publish_weekly, true);
});

test("heartbeat external trigger carries the same logical date and expected slot", () => {
  const task = taskForCron("17 14 * * *", Date.parse("2026-10-15T14:17:00Z"));
  assert.equal(task.kind, "heartbeat");
  assert.equal(task.logicalDate, "2026-10-15");
  assert.equal(task.scheduledFor, "2026-10-15T22:17:00+08:00");
});

test("dispatch is correlated and polling waits for the actual run start", async () => {
  const task = taskForCron("35 11 * * 1-5", Date.parse("2026-10-15T11:35:00Z"));
  const requests = [];
  let polls = 0;
  const request = async (url, init = {}) => {
    requests.push({ url, init });
    if (url.includes("/dispatches")) return { ok: true, status: 204 };
    polls += 1;
    if (polls === 1) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
    const run = { display_title: "public-market-brief 2026-10-15 " + task.triggerId,
      html_url: "https://github.com/Evanlei2025/market-advisor/actions/runs/123" };
    if (polls === 2) Object.assign(run, { status: "queued", run_started_at: null });
    else Object.assign(run, { id: 123, status: "in_progress", run_started_at: "2026-10-15T11:37:00Z" });
    return { ok: true, status: 200, json: async () => ({ workflow_runs: [run] }) };
  };
  const started = await ensureStarted(task, { GITHUB_TOKEN: "test" }, {
    request, wait: async () => {}, timeoutMs: 100, pollIntervalMs: 1,
  });
  assert.equal(started.id, 123);
  assert.equal(requests.filter(({ url }) => url.includes("/dispatches")).length, 1);
  const body = JSON.parse(requests.find(({ url }) => url.includes("/dispatches")).init.body);
  assert.equal(body.inputs.logical_date, "2026-10-15");
  assert.equal(body.inputs.scheduled_for, "2026-10-15T19:35:00+08:00");
  assert.equal(body.inputs.trigger_id, task.triggerId);
});

test("an existing matching run is polled without submitting another dispatch", async () => {
  const task = taskForCron("17 14 * * *", Date.parse("2026-10-15T14:17:00Z"));
  const requests = [];
  const request = async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => ({ workflow_runs: [{
      id: 321, display_title: "public-service-heartbeat 2026-10-15 " + task.triggerId,
      status: "completed", run_started_at: "2026-10-15T14:18:00Z",
    }] }) };
  };
  const run = await ensureStarted(task, { GITHUB_TOKEN: "test" }, { request, timeoutMs: 1 });
  assert.equal(run.id, 321);
  assert.equal(requests.some((url) => url.includes("/dispatches")), false);
});

test("lack of an in-progress or completed run fails the start check", async () => {
  const task = taskForCron("35 11 * * 1-5", Date.parse("2026-10-15T11:35:00Z"));
  let polls = 0;
  const request = async (url) => {
    if (url.includes("/dispatches")) return { ok: true, status: 204 };
    polls += 1;
    return { ok: true, status: 200, json: async () => ({ workflow_runs: [{
      display_title: "public-market-brief 2026-10-15 " + task.triggerId,
      status: "queued", run_started_at: null,
    }] }) };
  };
  await assert.rejects(ensureStarted(task, { GITHUB_TOKEN: "test" }, {
    request, wait: async () => {}, timeoutMs: 1, pollIntervalMs: 1,
  }), /did not start/);
  assert.ok(polls >= 1);
});
