"use strict";

const elements = Object.fromEntries(
  [...document.querySelectorAll("[id]")].map((element) => [element.id, element]),
);
const activeStatuses = new Set(["preparing", "running"]);
const state = {
  csrfToken: null,
  capabilities: null,
  tasks: new Map(),
  nextCursor: null,
  selectedId: null,
  renderedId: null,
  renderedInputs: null,
  renderedArtifactUrl: null,
  refreshTimer: null,
};

function makeElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function showDashboard(authenticated) {
  elements.loginPanel.hidden = authenticated;
  elements.dashboard.hidden = !authenticated;
  elements.logoutButton.hidden = !authenticated;
  elements.connectionState.textContent = authenticated ? "已连接" : "未登录";
  if (!authenticated) {
    state.csrfToken = null;
    state.capabilities = null;
    state.tasks.clear();
    state.nextCursor = null;
    state.selectedId = null;
    state.renderedId = null;
    state.renderedInputs = null;
    state.renderedArtifactUrl = null;
    elements.artifactVideo.pause();
    elements.artifactVideo.removeAttribute("src");
    elements.artifactVideo.load();
    elements.detailInputs.replaceChildren();
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

async function readResponse(response) {
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error(`服务返回了无效响应 (${response.status})`);
  }
  if (!response.ok) {
    const message = body?.error?.message || `请求失败 (${response.status})`;
    if (response.status === 401) showDashboard(false);
    throw new Error(message);
  }
  return body;
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrfToken || "");
  const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  return readResponse(response);
}

function sameOriginUrl(path) {
  if (typeof path !== "string") return null;
  const url = new URL(path, window.location.origin);
  return url.origin === window.location.origin && url.pathname.startsWith("/v1/tasks/") ? url.href : null;
}

function setConnectionError(error) {
  elements.connectionState.textContent = "连接失败";
  elements.taskFormMessage.textContent = error.message;
  elements.taskFormMessage.className = "formMessage errorText";
}

async function initialize() {
  try {
    const response = await fetch("/admin/session", { credentials: "same-origin", cache: "no-store" });
    const session = await readResponse(response);
    showDashboard(session.authenticated);
    if (session.authenticated) {
      state.csrfToken = session.csrfToken;
      await loadDashboard();
    }
  } catch (error) {
    showDashboard(false);
    elements.loginError.textContent = error.message;
  }
}

async function loadDashboard() {
  await Promise.all([loadCapabilities(), loadTasks()]);
  if (!state.refreshTimer) {
    state.refreshTimer = setInterval(() => {
      refreshDashboard().catch(setConnectionError);
    }, 5000);
  }
}

async function refreshDashboard() {
  await Promise.all([loadCapabilities(), loadTasks()]);
  if (state.selectedId) await loadDetail(state.selectedId);
  elements.connectionState.textContent = "已连接";
}

async function login(event) {
  event.preventDefault();
  elements.loginError.textContent = "";
  const token = elements.tokenInput.value;
  try {
    const session = await readResponse(await fetch("/admin/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      credentials: "same-origin",
      cache: "no-store",
    }));
    elements.loginForm.reset();
    state.csrfToken = session.csrfToken;
    showDashboard(true);
    await loadDashboard();
  } catch (error) {
    elements.loginError.textContent = error.message;
  }
}

async function logout() {
  try {
    await apiFetch("/admin/session", { method: "DELETE" });
    showDashboard(false);
  } catch (error) {
    elements.taskFormMessage.textContent = error.message;
    elements.taskFormMessage.className = "formMessage errorText";
  }
}

function setInputRange(input, range) {
  if (!range || typeof range !== "object") return;
  if (range.min !== undefined) input.min = range.min;
  if (range.max !== undefined) input.max = range.max;
  if (range.multiple !== undefined) input.step = range.multiple;
}

function applyCapabilities(data) {
  const defaults = data.taskDefaults || {};
  const ranges = data.parameterRanges || {};
  if (!state.capabilities) {
    for (const input of document.querySelectorAll("[data-parameter]")) {
      const key = input.dataset.parameter;
      if (defaults[key] !== undefined) input.value = defaults[key];
      setInputRange(input, ranges[key]);
    }
    const durationMode = defaults.frames !== undefined ? "frames" : "seconds";
    elements.durationMode.value = durationMode;
    elements.durationInput.value = defaults[durationMode] ?? "";
  }
  state.capabilities = data;
  setDurationRange();
  elements.activeCount.textContent = `${data.activeJobs} / ${data.maxConcurrentJobs}`;
  elements.referenceLimit.textContent = `最多 ${data.conditioningLimits?.references ?? 12} 个参考；图片 ${data.conditioningLimits?.images ?? 9}、视频 ${data.conditioningLimits?.videos ?? 3}、音频 ${data.conditioningLimits?.audio ?? 3}。`;
  elements.parameterHint.textContent = `宽 × 高不超过 ${ranges.maxPixels ?? 1032192} 像素，尺寸为 ${ranges.width?.multiple ?? 32} 的倍数；reuse 和 coreReuse 不能同时大于 1。`;
  updateAddReferenceButton();
}

async function loadCapabilities() {
  applyCapabilities(await apiFetch("/v1/capabilities"));
}

function setDurationRange() {
  const mode = elements.durationMode.value;
  const range = state.capabilities?.parameterRanges?.[mode];
  elements.durationInput.step = mode === "frames" ? "1" : "any";
  elements.durationInput.min = range?.min ?? (mode === "frames" ? 22 : 0.001);
  elements.durationInput.max = range?.max ?? (mode === "frames" ? 362 : 15.083333333333334);
}

function changeDurationMode() {
  const mode = elements.durationMode.value;
  elements.durationInput.value = state.capabilities?.taskDefaults?.[mode] ?? (mode === "frames" ? 56 : 4);
  setDurationRange();
}

function changeConditioningMode() {
  elements.frameFields.hidden = elements.conditioningMode.value !== "frames";
  elements.referenceFields.hidden = elements.conditioningMode.value !== "references";
}

function updateReferenceRow(row) {
  const kind = row.querySelector("select").value;
  const media = row.querySelector(".referenceFile");
  const audio = row.querySelector(".referenceAudio");
  media.accept = kind === "image" ? "image/*" : kind === "audio" ? "audio/*" : "video/*";
  audio.hidden = kind !== "videoWithAudio";
  audio.required = kind === "videoWithAudio";
}

function updateAddReferenceButton() {
  const max = state.capabilities?.conditioningLimits?.references ?? 12;
  elements.addReferenceButton.disabled = elements.referenceList.children.length >= max;
}

function addReference() {
  if (elements.addReferenceButton.disabled) return;
  const row = makeElement("div", "referenceRow");
  const header = makeElement("div", "referenceRowHeader");
  const number = makeElement("strong", "referenceNumber");
  const actions = makeElement("div", "referenceActions");
  const up = makeElement("button", "iconButton", "↑");
  const down = makeElement("button", "iconButton", "↓");
  const remove = makeElement("button", "iconButton", "删除");
  for (const button of [up, down, remove]) button.type = "button";
  up.setAttribute("aria-label", "上移参考");
  down.setAttribute("aria-label", "下移参考");
  up.addEventListener("click", () => { if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling); renumberReferences(); });
  down.addEventListener("click", () => { if (row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row); renumberReferences(); });
  remove.addEventListener("click", () => { row.remove(); renumberReferences(); });
  actions.append(up, down, remove);
  header.append(number, actions);
  const kind = makeElement("select", "referenceKind");
  kind.setAttribute("aria-label", "参考类型");
  for (const [value, label] of [
    ["image", "图片"], ["silentVideo", "无声视频"], ["video", "有声视频"],
    ["audio", "音频"], ["videoWithAudio", "视频 + 独立音频"],
  ]) {
    const option = makeElement("option", "", label);
    option.value = value;
    kind.append(option);
  }
  const file = makeElement("input", "referenceFile");
  file.type = "file";
  file.required = true;
  file.setAttribute("aria-label", "参考文件");
  const audio = makeElement("input", "referenceAudio");
  audio.type = "file";
  audio.accept = "audio/*";
  audio.setAttribute("aria-label", "独立音频文件");
  kind.addEventListener("change", () => updateReferenceRow(row));
  row.append(header, kind, file, audio);
  elements.referenceList.append(row);
  updateReferenceRow(row);
  renumberReferences();
}

function renumberReferences() {
  [...elements.referenceList.children].forEach((row, index) => {
    row.querySelector(".referenceNumber").textContent = `参考 ${index + 1}`;
  });
  updateAddReferenceButton();
}

function validateParameters(parameters) {
  const ranges = state.capabilities?.parameterRanges || {};
  for (const name of ["width", "height", "layers", "steps", "reuse", "coreReuse", "frames"]) {
    if (parameters[name] === undefined) continue;
    const value = parameters[name];
    const range = ranges[name] || {};
    if (!Number.isSafeInteger(value) || value < (range.min ?? 1) || value > (range.max ?? Number.MAX_SAFE_INTEGER)) {
      throw new Error(`${name} 超出允许范围`);
    }
    if (range.multiple && value % range.multiple !== 0) throw new Error(`${name} 必须是 ${range.multiple} 的倍数`);
  }
  if (parameters.width * parameters.height > (ranges.maxPixels ?? 1032192)) throw new Error("输出尺寸超过像素上限");
  if (parameters.reuse > 1 && parameters.coreReuse > 1) throw new Error("reuse 和 coreReuse 不能同时大于 1");
  if (!/^[0-9]+$/.test(parameters.seed) || BigInt(parameters.seed) > 18446744073709551615n) throw new Error("seed 必须是 0 到 2⁶⁴−1 的整数");
  if (parameters.seconds !== undefined) {
    const limit = ranges.seconds || {};
    if (!Number.isFinite(parameters.seconds) || parameters.seconds <= (limit.minExclusive ?? 0) || parameters.seconds > (limit.max ?? 15.083333333333334)) {
      throw new Error("秒数超出允许范围");
    }
  }
}

function collectParameters() {
  const parameters = {};
  for (const input of document.querySelectorAll("[data-parameter]")) {
    const key = input.dataset.parameter;
    parameters[key] = key === "seed" ? input.value.trim() : Number(input.value);
  }
  parameters[elements.durationMode.value] = Number(elements.durationInput.value);
  validateParameters(parameters);
  return parameters;
}

function collectConditioning(form) {
  const mode = elements.conditioningMode.value;
  if (mode === "text") return undefined;
  if (mode === "frames") {
    const first = elements.firstFrameInput.files[0];
    const last = elements.lastFrameInput.files[0];
    if (!first && !last) throw new Error("请选择首帧或尾帧图片");
    const conditioning = {};
    if (first) { conditioning.firstFrameFileKey = "firstFrame"; form.append("firstFrame", first); }
    if (last) { conditioning.lastFrameFileKey = "lastFrame"; form.append("lastFrame", last); }
    return conditioning;
  }
  const rows = [...elements.referenceList.children];
  if (!rows.length) throw new Error("请添加至少一个参考");
  const counts = { images: 0, videos: 0, audio: 0 };
  const references = rows.map((row, index) => {
    const kind = row.querySelector("select").value;
    const file = row.querySelector(".referenceFile").files[0];
    if (!file) throw new Error(`参考 ${index + 1} 未选择文件`);
    if (kind === "image") counts.images += 1;
    if (["silentVideo", "video", "videoWithAudio"].includes(kind)) counts.videos += 1;
    if (["audio", "video", "videoWithAudio"].includes(kind)) counts.audio += 1;
    const fileKey = `ref${index}`;
    form.append(fileKey, file);
    const reference = { kind, fileKey };
    if (kind === "videoWithAudio") {
      const audio = row.querySelector(".referenceAudio").files[0];
      if (!audio) throw new Error(`参考 ${index + 1} 未选择独立音频`);
      reference.audioFileKey = `audio${index}`;
      form.append(reference.audioFileKey, audio);
    }
    return reference;
  });
  const limits = state.capabilities?.conditioningLimits || {};
  if (rows.length > (limits.references ?? 12) || counts.images > (limits.images ?? 9) || counts.videos > (limits.videos ?? 3) || counts.audio > (limits.audio ?? 3)) {
    throw new Error("参考数量超过模型限制");
  }
  return { references };
}

async function createTask(event) {
  event.preventDefault();
  elements.taskFormMessage.textContent = "";
  elements.submitTaskButton.disabled = true;
  try {
    const form = new FormData();
    const request = { prompt: elements.promptInput.value, parameters: collectParameters() };
    const conditioning = collectConditioning(form);
    if (conditioning) request.conditioning = conditioning;
    form.append("request", JSON.stringify(request));
    const result = await apiFetch("/v1/tasks", { method: "POST", body: form });
    elements.taskFormMessage.className = "formMessage successText";
    elements.taskFormMessage.textContent = `任务 ${result.id} 已创建，实际 ${result.actualFrames} 帧 / ${result.actualSeconds} 秒。`;
    await loadTasks();
    await loadDetail(result.id);
  } catch (error) {
    elements.taskFormMessage.className = "formMessage errorText";
    elements.taskFormMessage.textContent = error.message;
  } finally {
    elements.submitTaskButton.disabled = false;
  }
}

function taskSort(first, second) {
  return second.createdAt.localeCompare(first.createdAt) || second.id.localeCompare(first.id);
}

function renderTaskList(container, tasks) {
  container.replaceChildren();
  if (!tasks.length) {
    container.append(makeElement("p", "emptyState", "暂无任务"));
    return;
  }
  for (const task of tasks) {
    const button = makeElement("button", `taskItem${state.selectedId === task.id ? " selected" : ""}`);
    button.type = "button";
    button.addEventListener("click", () => loadDetail(task.id).catch(setConnectionError));
    const top = makeElement("span", "taskItemTop");
    top.append(makeElement("code", "", task.id.slice(0, 8)), makeElement("span", `statusBadge ${task.status}`, task.status));
    const prompt = task.request?.prompt || "";
    button.append(top, makeElement("span", "taskPrompt", prompt.slice(0, 90) || "正在准备"));
    const execution = task.execution || {};
    const settings = execution.layers && execution.steps ? `${execution.layers} layers · ${execution.steps} steps · ${execution.frames} 帧` : "";
    const meta = [task.phase, `${task.progressPercent ?? 0}%`, settings, task.createdAt].filter(Boolean).join(" · ");
    button.append(makeElement("span", "taskMeta", meta));
    container.append(button);
  }
}

function renderTasks() {
  const tasks = [...state.tasks.values()].sort(taskSort);
  const active = tasks.filter((task) => activeStatuses.has(task.status));
  renderTaskList(elements.activeTasks, active);
  renderTaskList(elements.historyTasks, tasks.filter((task) => !activeStatuses.has(task.status)));
  elements.runningCount.textContent = active.length;
  elements.totalCount.textContent = tasks.length + (state.nextCursor ? "+" : "");
  elements.loadMoreButton.hidden = !state.nextCursor;
}

async function loadTasks(cursor = null) {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) query.set("cursor", cursor);
  const result = await apiFetch(`/v1/tasks?${query}`);
  for (const task of result.items || []) state.tasks.set(task.id, task);
  if (cursor || state.nextCursor === null) state.nextCursor = result.nextCursor;
  renderTasks();
}

function formatJson(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function renderInputs(inputs) {
  elements.detailInputs.replaceChildren();
  if (!inputs?.length) {
    elements.detailInputs.append(makeElement("p", "emptyState", "无引用素材"));
    return;
  }
  for (const [index, input] of inputs.entries()) {
    const item = makeElement("div", "inputItem");
    const title = makeElement("div", "inputTitle", `${index + 1}. ${input.role}${input.referenceIndex === null || input.referenceIndex === undefined ? "" : ` #${input.referenceIndex + 1}`} · ${input.originalName}`);
    item.append(title);
    const url = sameOriginUrl(input.url);
    if (url) {
      const link = makeElement("a", "sourceLink", "打开原始素材");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
      let media;
      if (input.kind === "image" || ["firstFrame", "lastFrame"].includes(input.role)) media = makeElement("img", "inputPreview");
      else if (input.kind === "audio" || input.role === "referenceAudio") media = makeElement("audio", "inputPreview");
      else media = makeElement("video", "inputPreview");
      media.src = url;
      if (media.tagName === "IMG") media.alt = input.originalName;
      else media.controls = true;
      item.append(media);
    }
    elements.detailInputs.append(item);
  }
}

async function loadDetail(id) {
  state.selectedId = id;
  renderTasks();
  const task = await apiFetch(`/v1/tasks/${encodeURIComponent(id)}`);
  if (state.selectedId !== id) return;
  elements.detailEmpty.hidden = true;
  elements.detailContent.hidden = false;
  elements.detailId.textContent = task.id;
  elements.detailStatus.textContent = task.status;
  elements.detailStatus.className = `statusBadge ${task.status}`;
  elements.detailProgress.textContent = `${task.phase || task.status} · ${task.progressPercent ?? 0}%`;
  elements.detailDates.textContent = [task.createdAt && `创建：${task.createdAt}`, task.startedAt && `开始：${task.startedAt}`, task.finishedAt && `结束：${task.finishedAt}`].filter(Boolean).join(" · ");
  elements.detailError.hidden = !task.errorMessage;
  elements.detailError.textContent = task.errorMessage || "";
  elements.detailPrompt.textContent = task.request?.prompt || "";
  elements.detailRequest.textContent = formatJson(task.request);
  elements.detailExecution.textContent = formatJson(task.execution);
  const inputSignature = JSON.stringify(task.inputs || []);
  if (state.renderedId !== id || state.renderedInputs !== inputSignature) {
    renderInputs(task.inputs);
    state.renderedInputs = inputSignature;
  }
  const artifactUrl = sameOriginUrl(task.artifactUrl);
  elements.artifactSection.hidden = !artifactUrl;
  if (state.renderedId !== id || state.renderedArtifactUrl !== artifactUrl) {
    if (artifactUrl) {
      elements.artifactVideo.src = artifactUrl;
      elements.artifactDownload.href = artifactUrl;
      elements.artifactDownload.download = `${task.id}.mp4`;
    } else {
      elements.artifactVideo.removeAttribute("src");
      elements.artifactVideo.load();
      elements.artifactDownload.removeAttribute("href");
    }
    state.renderedArtifactUrl = artifactUrl;
  }
  state.renderedId = id;
  try {
    const log = await apiFetch(`/v1/tasks/${encodeURIComponent(id)}/log?tailBytes=65536`);
    if (state.selectedId === id) elements.detailLog.textContent = log.text || "暂无日志";
  } catch (error) {
    if (state.selectedId === id) elements.detailLog.textContent = error.message;
  }
}

elements.loginForm.addEventListener("submit", login);
elements.logoutButton.addEventListener("click", logout);
elements.refreshButton.addEventListener("click", () => refreshDashboard().catch(setConnectionError));
elements.loadMoreButton.addEventListener("click", () => loadTasks(state.nextCursor).catch(setConnectionError));
elements.conditioningMode.addEventListener("change", changeConditioningMode);
elements.durationMode.addEventListener("change", changeDurationMode);
elements.addReferenceButton.addEventListener("click", addReference);
elements.taskForm.addEventListener("submit", createTask);
initialize();
