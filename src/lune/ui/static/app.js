(() => {
  "use strict";

  const TEST_PHASE = "test";
  const LOCAL_LLM_REASONS = new Set(["local_llm_model_missing", "local_llm_runtime_missing"]);
  const MODEL_REASONS = new Set(["whisper_model_missing", "embedding_model_missing"]);
  const PERSONA_REASONS = new Set([
    "persona_missing",
    "persona_invalid",
    "persona_unconfigured",
  ]);
  const CONFIG_REASONS = new Set(["config_missing", "config_invalid"]);

  const STATUS_COPY = {
    listening: {
      label: "在聽",
      subtitle: "",
      tone: "accent",
    },
    thinking: {
      label: "在想",
      subtitle: "",
      tone: "accent",
    },
    speaking: {
      label: "Lune 正在說話",
      subtitle: "直接開口就可以打斷她",
      tone: "accent",
    },
    paused_unsafe_output: {
      label: "切到內建喇叭了，先停一下",
      subtitle: "用內建喇叭的話她會聽到自己的聲音，然後就亂掉了。接上耳機會自動接回去，不用重按。",
      tone: "yellow",
      action: { label: "我接好了", command: "check_audio_devices" },
    },
    degraded_tts: {
      label: "現在是系統的聲音",
      subtitle: "先用系統合成音頂著。講的內容完全一樣，只是聽起來不像她。",
      tone: "quiet",
      action: { label: "看看為什麼", command: "get_status" },
    },
    error: {
      label: "Lune 現在暫時無法繼續",
      subtitle: "先檢查看看，再回來繼續聊。",
      tone: "red",
      action: { label: "看看怎麼回事", command: "get_status" },
    },
  };

  const setupSteps = [
    { id: "local", number: "1", label: "確認本機 LLM" },
    { id: "models", number: "2", label: "準備本機模型" },
    { id: "persona", number: "3", label: "認識彼此" },
    { id: "audio", number: "4", label: "麥克風與耳機" },
    { id: "voice", number: "5", label: "她的聲音", optional: true },
  ];

  const refs = {
    app: document.getElementById("lune-app"),
    connection: document.getElementById("connection-status"),
    setupScreen: document.getElementById("setup-screen"),
    setupStepList: document.getElementById("setup-step-list"),
    setupContent: document.getElementById("setup-content"),
    workspace: document.getElementById("workspace-screen"),
    sidebar: document.getElementById("sidebar"),
    sidebarToggle: document.getElementById("sidebar-toggle"),
    mobileSidebar: document.getElementById("mobile-sidebar-button"),
    newThread: document.getElementById("new-thread-button"),
    threadList: document.getElementById("thread-list"),
    sidebarState: document.getElementById("sidebar-state"),
    viewKicker: document.getElementById("view-kicker"),
    topbarTitle: document.getElementById("topbar-title"),
    renameThread: document.getElementById("rename-thread-button"),
    deviceButton: document.getElementById("device-button"),
    deviceDot: document.getElementById("device-dot"),
    deviceLabel: document.getElementById("device-label"),
    callButton: document.getElementById("call-button"),
    viewRoot: document.getElementById("view-root"),
    toastRegion: document.getElementById("toast-region"),
    confirmDialog: document.getElementById("confirm-dialog"),
    confirmCopy: document.getElementById("confirm-dialog-copy"),
    renameDialog: document.getElementById("rename-dialog"),
    renameInput: document.getElementById("rename-thread-input"),
  };

  const state = {
    snapshot: emptySnapshot(),
    activeView: "chat",
    activeSettingsTab: "you",
    selectedThreadId: null,
    pendingThreadSelection: null,
    panelMode: "expanded",
    callPanel: null,
    callPanelParts: null,
    speakText: true,
    sidebarCollapsed: false,
    sidebarMobileOpen: false,
    memoryQuery: "",
    memorySearchTimer: null,
    pendingForgetId: null,
    pendingRenameThreadId: null,
    setupStepOverride: null,
    messageScrolls: new Map(),
    websocket: null,
    bootstrap: null,
    bootstrapRequestInFlight: false,
    nativeBridgeReady: false,
    retryBootstrapWhenReady: false,
    pending: new Map(),
    searchResults: null,
    timer: null,
    timerBase: 0,
    timerStartedAt: 0,
    ready: false,
  };

  function emptySnapshot() {
    return {
      setup: null,
      app: { state: "mic_off", phase: TEST_PHASE },
      threads: [],
      activeThreadId: null,
      call: {
        active: false,
        threadId: null,
        mode: "expanded",
        elapsedSeconds: 0,
        speakText: true,
      },
      device: { label: "音訊裝置準備中", status: "quiet" },
      messages: {},
      memories: [],
      profile: { name: "", context: "" },
      persona: {},
    };
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  // A snapshot always carries these two, including the shell state sent while
  // the local runtime is still starting; no command result looks like this.
  function isSnapshotShape(value) {
    return isObject(value) && "app" in value && "threads" in value;
  }

  function asObject(value) {
    return isObject(value) ? value : {};
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function stringValue(value, fallback = "") {
    if (typeof value === "string") {
      return value;
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
    return fallback;
  }

  function firstValue(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function boolValue(value, fallback = false) {
    return typeof value === "boolean" ? value : fallback;
  }

  function numberValue(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function addChildren(parent, children) {
    for (const child of asArray(children)) {
      if (child === null || child === undefined || child === false) {
        continue;
      }
      parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return parent;
  }

  function element(tagName, options = {}, children = []) {
    const node = document.createElement(tagName);
    if (options.className) {
      node.className = options.className;
    }
    if (options.text !== undefined) {
      node.textContent = stringValue(options.text);
    }
    if (options.id) {
      node.id = options.id;
    }
    if (options.type) {
      node.type = options.type;
    }
    if (options.value !== undefined && "value" in node) {
      node.value = stringValue(options.value);
    }
    if (options.checked !== undefined && "checked" in node) {
      node.checked = Boolean(options.checked);
    }
    if (options.disabled !== undefined && "disabled" in node) {
      node.disabled = Boolean(options.disabled);
    }
    if (options.placeholder !== undefined && "placeholder" in node) {
      node.placeholder = stringValue(options.placeholder);
    }
    if (options.name !== undefined && "name" in node) {
      node.name = stringValue(options.name);
    }
    if (options.dataset) {
      for (const [key, value] of Object.entries(options.dataset)) {
        if (value !== undefined && value !== null) {
          node.dataset[key] = stringValue(value);
        }
      }
    }
    if (options.attrs) {
      for (const [key, value] of Object.entries(options.attrs)) {
        if (value !== undefined && value !== null) {
          node.setAttribute(key, stringValue(value));
        }
      }
    }
    return addChildren(node, children);
  }

  function clear(node) {
    node.replaceChildren();
  }

  function normalizeSnapshot(rawSnapshot) {
    const raw = asObject(rawSnapshot);
    const appRaw = asObject(firstValue(raw.app, raw.status));
    const setupPresent = Object.prototype.hasOwnProperty.call(raw, "setup") && raw.setup !== null;
    const setupRaw = setupPresent ? asObject(raw.setup) : null;
    const appState = stringValue(
      firstValue(raw.state, appRaw.state, raw.app_state, raw.appState),
      "mic_off",
    );
    const provider = stringValue(firstValue(appRaw.provider, raw.provider));
    const phase = stringValue(firstValue(appRaw.phase, raw.phase, raw.mode), TEST_PHASE);
    const isTestPhase =
      boolValue(firstValue(appRaw.test_phase, appRaw.testPhase, raw.test_phase), false) ||
      (phase !== "hybrid" && provider !== "openai_responses");
    const rawThreads = asArray(firstValue(raw.threads, raw.conversations));
    const threads = rawThreads.map(normalizeThread).filter((thread) => thread.id);
    const activeThreadId =
      stringValue(firstValue(raw.active_thread_id, raw.activeThreadId, raw.active_thread)) || null;
    const messages = normalizeMessages(raw, threads, activeThreadId);
    const rawCall = asObject(raw.call);
    const call = {
      active: boolValue(firstValue(rawCall.active, raw.call_active, raw.callActive), false),
      threadId: stringValue(firstValue(rawCall.thread_id, rawCall.threadId, raw.call_thread_id)) || null,
      mode: stringValue(firstValue(rawCall.mode, rawCall.panel_mode, raw.call_panel_mode), "expanded"),
      readonly: boolValue(firstValue(rawCall.readonly, rawCall.read_only, raw.call_readonly), false),
      elapsedSeconds: Math.max(
        0,
        numberValue(firstValue(rawCall.elapsed_seconds, rawCall.elapsedSeconds, raw.call_elapsed_seconds)),
      ),
      speakText: boolValue(
        firstValue(rawCall.speak_text, rawCall.speakText, raw.text_to_speech, raw.textToSpeech),
        true,
      ),
    };
    const rawDevice = asObject(raw.device);
    const device = {
      label: stringValue(firstValue(rawDevice.label, rawDevice.name, raw.device_label), "音訊裝置準備中"),
      status: stringValue(firstValue(rawDevice.status, rawDevice.state), "quiet"),
      outputBuiltin: boolValue(
        firstValue(rawDevice.output_builtin, rawDevice.outputBuiltin, rawDevice.unsafe),
        false,
      ),
    };
    const profileRaw = asObject(firstValue(raw.profile, raw.user_profile, raw.userProfile));
    const personaRaw = asObject(firstValue(raw.persona, raw.lune_profile, raw.luneProfile));

    return {
      setup: setupRaw
        ? {
            ...setupRaw,
            reasons: uniqueStrings(firstValue(setupRaw.reasons, raw.reasons)),
            steps: normalizeSetupSteps(setupRaw.steps),
            downloads: asArray(firstValue(setupRaw.downloads, raw.downloads)).map(normalizeDownload),
            currentStep: stringValue(
              firstValue(setupRaw.current_step, setupRaw.currentStep),
            ),
          }
        : null,
      app: {
        state: appState,
        phase: isTestPhase ? TEST_PHASE : phase,
        provider,
        isTestPhase,
      },
      threads,
      activeThreadId,
      call,
      device,
      messages,
      memories: asArray(firstValue(raw.memories, raw.memory_items)).map(normalizeMemory),
      profile: {
        name: stringValue(firstValue(profileRaw.name, profileRaw.user_name, profileRaw.userName)),
        context: stringValue(
          firstValue(profileRaw.context, profileRaw.about, profileRaw.always_include),
        ),
      },
      persona: {
        chineseRatio: normalizeRatio(
          firstValue(personaRaw.chinese_ratio, personaRaw.chineseRatio, personaRaw.language_ratio),
        ),
        initiative: normalizeInitiative(firstValue(personaRaw.initiative, personaRaw.proactivity)),
        responseLength: stringValue(
          firstValue(personaRaw.response_length, personaRaw.responseLength, personaRaw.length),
          "normal",
        ),
        voice: stringValue(firstValue(personaRaw.voice, personaRaw.voice_mode, personaRaw.voiceMode), "system"),
      },
    };
  }

  function normalizeThread(rawThread) {
    const raw = asObject(rawThread);
    return {
      id: stringValue(firstValue(raw.id, raw.thread_id, raw.threadId)),
      title: stringValue(firstValue(raw.title, raw.name), "未命名對話"),
      group: stringValue(firstValue(raw.group, raw.bucket, raw.date_group)),
      updatedAt: stringValue(firstValue(raw.updated_at, raw.updatedAt, raw.created_at)),
      messages: asArray(raw.messages).map(normalizeMessage),
    };
  }

  function normalizeMessages(raw, threads, activeThreadId) {
    const byThread = {};
    for (const thread of threads) {
      if (thread.messages.length > 0) {
        byThread[thread.id] = thread.messages.slice();
      }
    }
    const rawMessages = firstValue(raw.messages, raw.message_map, raw.messageMap);
    if (Array.isArray(rawMessages)) {
      for (const message of rawMessages.map(normalizeMessage)) {
        const threadId = message.threadId || activeThreadId;
        if (!threadId) {
          continue;
        }
        appendUniqueMessage(byThread, threadId, { ...message, threadId });
      }
    } else if (isObject(rawMessages)) {
      for (const [threadId, messages] of Object.entries(rawMessages)) {
        for (const message of asArray(messages).map(normalizeMessage)) {
          appendUniqueMessage(byThread, threadId, { ...message, threadId });
        }
      }
    }
    return byThread;
  }

  function normalizeMessage(rawMessage) {
    const raw = asObject(rawMessage);
    const roleRaw = stringValue(firstValue(raw.role, raw.author, raw.sender), "assistant").toLowerCase();
    return {
      id: stringValue(firstValue(raw.id, raw.message_id, raw.messageId)),
      threadId: stringValue(firstValue(raw.thread_id, raw.threadId)),
      role: roleRaw === "user" || roleRaw === "you" ? "user" : "assistant",
      text: stringValue(firstValue(raw.text, raw.content, raw.message)),
      createdAt: stringValue(firstValue(raw.created_at, raw.createdAt, raw.timestamp)),
      memoryIds: asArray(firstValue(raw.memory_ids, raw.memoryIds, raw.memories))
        .map((id) => stringValue(id))
        .filter(Boolean),
    };
  }

  function appendUniqueMessage(target, threadId, message) {
    const messages = target[threadId] || [];
    const hasMatch = message.id
      ? messages.some((item) => item.id === message.id)
      : messages.some(
          (item) =>
            item.text === message.text &&
            item.createdAt === message.createdAt &&
            item.role === message.role,
        );
    if (!hasMatch) {
      messages.push(message);
      target[threadId] = messages;
    }
  }

  function normalizeMemory(rawMemory) {
    const raw = asObject(rawMemory);
    return {
      id: stringValue(firstValue(raw.id, raw.memory_id, raw.memoryId)),
      content: stringValue(firstValue(raw.content, raw.text)),
      source: stringValue(firstValue(raw.source, raw.origin), "observed"),
      importance: normalizeImportance(raw.importance),
      similarity: numberValue(firstValue(raw.similarity, raw.score), NaN),
      match: stringValue(raw.match),
      createdAt: stringValue(firstValue(raw.created_at, raw.createdAt, raw.timestamp)),
    };
  }

  function normalizeDownload(rawDownload) {
    const raw = asObject(rawDownload);
    const progress = numberValue(firstValue(raw.progress, raw.percent), NaN);
    return {
      id: stringValue(firstValue(raw.id, raw.name, raw.model)),
      label: stringValue(firstValue(raw.label, raw.name, raw.model), "本機模型"),
      progress: Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : null,
      state: stringValue(firstValue(raw.state, raw.status)),
    };
  }

  function normalizeRatio(rawValue) {
    const value = numberValue(rawValue, 70);
    return Math.max(0, Math.min(100, value <= 1 ? value * 100 : value));
  }

  function normalizeInitiative(value) {
    const raw = stringValue(value, "balanced");
    if (["安靜", "gentle"].includes(raw)) {
      return "gentle";
    }
    if (["主動", "proactive"].includes(raw)) {
      return "proactive";
    }
    return "balanced";
  }

  function normalizeImportance(value) {
    if (typeof value === "string") {
      if (["strong", "high"].includes(value)) {
        return 0.8;
      }
      if (["soft", "low"].includes(value)) {
        return 0.2;
      }
      return 0.5;
    }
    return Math.max(0, Math.min(1, numberValue(value, 0.5)));
  }

  function normalizeSetupSteps(value) {
    const steps = {};
    if (Array.isArray(value)) {
      for (const rawStep of value) {
        const raw = asObject(rawStep);
        const id = normalizeSetupStepId(firstValue(raw.id, raw.key));
        if (id) {
          steps[id] = raw;
        }
      }
      return steps;
    }
    for (const [rawId, rawStep] of Object.entries(asObject(value))) {
      const id = normalizeSetupStepId(rawId);
      if (id) {
        steps[id] = asObject(rawStep);
      }
    }
    return steps;
  }

  function normalizeSetupStepId(value) {
    const raw = stringValue(value);
    const aliases = {
      "1": "local",
      "2": "models",
      "3": "persona",
      "4": "audio",
      "5": "voice",
      local_runtime: "local",
      local_models: "models",
      microphone: "audio",
    };
    if (raw === "repair") {
      return raw;
    }
    return aliases[raw] || (setupSteps.some((step) => step.id === raw) ? raw : "");
  }

  function uniqueStrings(values) {
    return [...new Set(asArray(values).map((value) => stringValue(value)).filter(Boolean))];
  }

  function applySnapshot(rawSnapshot) {
    const previous = state.snapshot;
    const next = normalizeSnapshot(rawSnapshot);
    state.snapshot = next;

    if (next.setup === null) {
      state.setupStepOverride = null;
    } else if (
      state.setupStepOverride &&
      isSetupStepComplete(state.setupStepOverride, next.setup)
    ) {
      state.setupStepOverride = null;
    }

    if (
      !previous.call.active ||
      previous.call.threadId !== next.call.threadId ||
      (!next.call.active && previous.call.active)
    ) {
      state.panelMode = next.call.mode === "collapsed" ? "collapsed" : "expanded";
    }
    state.speakText = next.call.speakText;
    const pendingThreadId = state.pendingThreadSelection;
    if (pendingThreadId && next.threads.some((thread) => thread.id === pendingThreadId)) {
      if (next.activeThreadId === pendingThreadId) {
        state.pendingThreadSelection = null;
      } else {
        state.selectedThreadId = pendingThreadId;
      }
    } else if (pendingThreadId) {
      state.pendingThreadSelection = null;
    }
    if (
      !state.pendingThreadSelection &&
      next.activeThreadId &&
      next.threads.some((thread) => thread.id === next.activeThreadId)
    ) {
      state.selectedThreadId = next.activeThreadId;
    } else if (!state.pendingThreadSelection && !next.threads.some((thread) => thread.id === state.selectedThreadId)) {
      state.selectedThreadId = next.threads[0]?.id || null;
    }
    if (state.activeView === "chat" && !state.selectedThreadId && next.threads.length > 0) {
      state.selectedThreadId = next.threads[0].id;
    }
    updateTimerSource();
    render();
  }

  function mergeEvent(eventName, payload) {
    const name = stringValue(eventName).toLowerCase();
    const body = asObject(payload);
    if (body.snapshot || name === "snapshot" || name === "snapshot_changed") {
      applySnapshot(body.snapshot || payload);
      return;
    }

    const raw = exportSnapshotForPatch();
    switch (name) {
      case "state_changed":
        raw.app = { ...asObject(raw.app), ...body };
        break;
      case "device_changed":
        raw.device = { ...asObject(raw.device), ...body };
        break;
      case "call_changed":
      case "call_state_changed":
        raw.call = { ...asObject(raw.call), ...body };
        break;
      case "thread_created":
      case "thread_updated": {
        const item = body.thread || body;
        const id = stringValue(firstValue(item.id, item.thread_id, item.threadId));
        const existing = asArray(raw.threads);
        // The snapshot arrives ordered by `updated_at` descending and the
        // sidebar renders that order as-is, so a touched thread goes to the
        // head rather than the tail.
        raw.threads = id
          ? [item, ...existing.filter((thread) => stringValue(thread.id) !== id)]
          : existing;
        if (name === "thread_created" && id) {
          raw.active_thread_id = id;
        }
        break;
      }
      case "thread_deleted": {
        const id = stringValue(firstValue(body.id, body.thread_id, body.threadId));
        raw.threads = asArray(raw.threads).filter((thread) => stringValue(thread.id) !== id);
        if (raw.active_thread_id === id) {
          raw.active_thread_id = null;
        }
        break;
      }
      case "message_added": {
        const message = body.message || body;
        const threadId = stringValue(firstValue(message.thread_id, message.threadId, body.thread_id));
        if (threadId) {
          const entries = asObject(raw.messages);
          entries[threadId] = [...asArray(entries[threadId]), message];
          raw.messages = entries;
        }
        break;
      }
      case "memory_updated":
      case "memories_changed":
      case "memory_list_changed":
        raw.memories = asArray(firstValue(body.memories, body.items));
        break;
      case "memory_deleted": {
        const id = stringValue(firstValue(body.id, body.memory_id, body.memoryId));
        raw.memories = asArray(raw.memories).filter((memory) => stringValue(memory.id) !== id);
        break;
      }
      case "profile_changed":
        raw.profile = { ...asObject(raw.profile), ...body };
        break;
      case "persona_changed":
        raw.persona = { ...asObject(raw.persona), ...body };
        break;
      case "setup_changed":
        raw.setup = body;
        break;
      case "setup_completed":
        raw.setup = null;
        break;
      default:
        return;
    }
    applySnapshot(raw);
  }

  function exportSnapshotForPatch() {
    const snapshot = state.snapshot;
    return {
      setup: snapshot.setup,
      app: { ...snapshot.app },
      threads: snapshot.threads.map((thread) => ({
        id: thread.id,
        title: thread.title,
        group: thread.group,
        updated_at: thread.updatedAt,
      })),
      active_thread_id: snapshot.activeThreadId,
      call: {
        active: snapshot.call.active,
        thread_id: snapshot.call.threadId,
        mode: snapshot.call.mode,
        readonly: snapshot.call.readonly,
        // `applySnapshot` re-bases the local timer from this value.  With the
        // whole snapshot now arriving rarely, replaying a stale count on every
        // merged event would visibly rewind the call clock.
        elapsed_seconds: snapshot.call.active ? currentElapsed() : snapshot.call.elapsedSeconds,
        speak_text: snapshot.call.speakText,
      },
      device: {
        label: snapshot.device.label,
        status: snapshot.device.status,
        output_builtin: snapshot.device.outputBuiltin,
      },
      messages: snapshot.messages,
      memories: snapshot.memories,
      profile: {
        name: snapshot.profile.name,
        context: snapshot.profile.context,
      },
      persona: {
        chinese_ratio: snapshot.persona.chineseRatio,
        initiative: snapshot.persona.initiative,
        response_length: snapshot.persona.responseLength,
        voice: snapshot.persona.voice,
      },
    };
  }

  function render() {
    const setupActive = state.snapshot.setup !== null;
    refs.setupScreen.hidden = !setupActive;
    refs.workspace.hidden = setupActive;
    refs.app.setAttribute("aria-busy", state.ready ? "false" : "true");
    if (setupActive) {
      renderSetup();
      return;
    }
    renderSidebar();
    renderTopbar();
    renderCurrentView();
  }

  function renderSidebar() {
    refs.workspace.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
    refs.workspace.classList.toggle("sidebar-mobile-open", state.sidebarMobileOpen);
    refs.sidebarToggle.setAttribute("aria-expanded", String(!state.sidebarCollapsed));
    refs.sidebarToggle.setAttribute(
      "aria-label",
      state.sidebarCollapsed ? "展開側邊欄" : "收合側邊欄",
    );
    refs.sidebarToggle.title = state.sidebarCollapsed ? "展開側邊欄" : "收合側邊欄";
    refs.sidebarState.textContent = sidebarStateText();
    const callActive = state.snapshot.call.active;
    refs.newThread.disabled = callActive;
    refs.newThread.title = callActive
      ? "通話中不能建立新對話；先掛斷後再開始新的對話。"
      : "建立新對話";
    clear(refs.threadList);
    const grouped = groupThreads(state.snapshot.threads);
    if (grouped.length === 0) {
      refs.threadList.append(
        element("p", { className: "field-hint sidebar-copy", text: "還沒有對話" }),
      );
    }
    for (const [groupName, threads] of grouped) {
      const group = element("section", { className: "thread-group" });
      group.append(element("span", { className: "thread-group-label", text: groupName }));
      for (const thread of threads) {
        const active = thread.id === state.selectedThreadId && state.activeView === "chat";
        group.append(
          element("button", {
            className: `thread-row${active ? " is-active" : ""}`,
            type: "button",
            text: thread.title,
            dataset: { action: "select-thread", threadId: thread.id },
            attrs: { "aria-current": active ? "page" : null, title: thread.title },
          }),
        );
      }
      refs.threadList.append(group);
    }
    for (const link of document.querySelectorAll(".sidebar-link")) {
      const view = link.dataset.view;
      link.classList.toggle("is-active", view === state.activeView);
      link.setAttribute("aria-current", view === state.activeView ? "page" : "false");
    }
  }

  function sidebarStateText() {
    if (!state.ready) {
      return "連線中";
    }
    const appState = state.snapshot.app.state;
    if (appState === "mic_off") {
      return "不通話";
    }
    return statusFor(appState).label;
  }

  function groupThreads(threads) {
    const groups = new Map();
    for (const thread of threads) {
      const name = thread.group || dateGroup(thread.updatedAt);
      if (!groups.has(name)) {
        groups.set(name, []);
      }
      groups.get(name).push(thread);
    }
    return [...groups.entries()];
  }

  function dateGroup(rawDate) {
    const date = new Date(rawDate);
    if (Number.isNaN(date.valueOf())) {
      return "較早";
    }
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).valueOf();
    const elapsedDays = Math.floor((midnight - new Date(date.getFullYear(), date.getMonth(), date.getDate()).valueOf()) / 86400000);
    if (elapsedDays <= 0) {
      return "今天";
    }
    if (elapsedDays < 7) {
      return "這禮拜";
    }
    return "更早";
  }

  function renderTopbar() {
    const title = currentTitle();
    const labels = {
      chat: ["對話", title],
      memories: ["記憶", "Lune 記得的事"],
      profile: ["設定檔", "你與 Lune 的設定檔"],
      settings: ["設定", "設定"],
    };
    const [kicker, heading] = labels[state.activeView] || labels.chat;
    refs.viewKicker.textContent = kicker;
    refs.topbarTitle.textContent = heading;
    refs.deviceLabel.textContent = state.snapshot.device.label;
    const deviceTone =
      state.snapshot.app.state === "paused_unsafe_output" || state.snapshot.device.outputBuiltin
        ? "yellow"
        : state.snapshot.app.state === "error"
          ? "red"
          : state.snapshot.call.active
            ? "accent"
            : "quiet";
    setDotTone(refs.deviceDot, deviceTone);
    const call = state.snapshot.call;
    refs.callButton.disabled = state.snapshot.app.state === "setup_required";
    refs.callButton.textContent = !call.active
      ? "打給 Lune"
      : call.threadId && call.threadId !== state.selectedThreadId
        ? "回到通話"
        : "通話中";
    const canRename =
      state.activeView === "chat" && Boolean(state.selectedThreadId) && !isReadonlyThread();
    refs.renameThread.hidden = !canRename;
    refs.renameThread.disabled = !canRename;
  }

  function currentTitle() {
    return (
      state.snapshot.threads.find((thread) => thread.id === state.selectedThreadId)?.title || "Lune"
    );
  }

  function renderCurrentView() {
    rememberMessageScroll(refs.viewRoot.querySelector(".messages-scroll"));
    clear(refs.viewRoot);
    switch (state.activeView) {
      case "memories":
        refs.viewRoot.append(renderMemories());
        break;
      case "profile":
        refs.viewRoot.append(renderProfile());
        break;
      case "settings":
        refs.viewRoot.append(renderSettings());
        break;
      default:
        refs.viewRoot.append(renderChat());
        break;
    }
  }

  function renderChat() {
    const view = element("section", { className: "chat-view", attrs: { "aria-label": "對話" } });
    const call = state.snapshot.call;
    const selectedThreadId = state.selectedThreadId;
    if (!call.active) {
      const banner = renderStatusBanner();
      if (banner) {
        view.append(banner);
      }
    }
    if (call.active) {
      view.append(renderCallPanel());
    } else {
      releaseCallPanel();
    }
    const readonly = isReadonlyThread();
    if (readonly) {
      view.append(renderReadonlyBanner());
    }
    const messagesScroll = element("div", {
      className: "messages-scroll",
      dataset: { threadId: selectedThreadId || "" },
    });
    const messages = element("div", { className: "messages" });
    const selectedMessages = state.snapshot.messages[selectedThreadId] || [];
    if (selectedMessages.length === 0) {
      messages.append(renderChatEmptyState());
    } else {
      for (const message of selectedMessages) {
        messages.append(renderMessage(message));
      }
    }
    messagesScroll.append(messages);
    bindChatScrollInteractions(messagesScroll);
    view.append(messagesScroll, renderComposer(readonly));
    queueMicrotask(() => {
      const saved = state.messageScrolls.get(selectedThreadId);
      if (!saved || saved.atBottom) {
        messagesScroll.scrollTop = messagesScroll.scrollHeight;
      } else {
        messagesScroll.scrollTop = Math.min(
          saved.top,
          Math.max(0, messagesScroll.scrollHeight - messagesScroll.clientHeight),
        );
      }
    });
    return view;
  }

  function rememberMessageScroll(messagesScroll) {
    if (!(messagesScroll instanceof HTMLElement)) {
      return;
    }
    const threadId = stringValue(messagesScroll.dataset.threadId);
    if (!threadId) {
      return;
    }
    const remaining = messagesScroll.scrollHeight - messagesScroll.clientHeight - messagesScroll.scrollTop;
    state.messageScrolls.set(threadId, {
      top: messagesScroll.scrollTop,
      atBottom: remaining < 24,
    });
  }

  function bindChatScrollInteractions(messagesScroll) {
    messagesScroll.addEventListener("scroll", () => rememberMessageScroll(messagesScroll), {
      passive: true,
    });
    messagesScroll.addEventListener(
      "wheel",
      (event) => {
        if (event.deltaY > 0) {
          collapseCallPanel();
        }
      },
      { passive: true },
    );
    let touchStartY = null;
    messagesScroll.addEventListener(
      "touchstart",
      (event) => {
        touchStartY = event.touches[0]?.clientY ?? null;
      },
      { passive: true },
    );
    messagesScroll.addEventListener(
      "touchmove",
      (event) => {
        const currentY = event.touches[0]?.clientY;
        if (touchStartY !== null && typeof currentY === "number" && currentY < touchStartY - 10) {
          collapseCallPanel();
          touchStartY = currentY;
        }
      },
      { passive: true },
    );
  }

  function renderStatusBanner() {
    const appState = state.snapshot.app.state;
    if (
      appState === "mic_off" ||
      appState === "setup_required" ||
      (state.snapshot.app.isTestPhase && ["degraded_llm", "budget_locked"].includes(appState))
    ) {
      return null;
    }
    const copy = statusFor(appState);
    const tone = copy.tone === "yellow" ? "yellow" : copy.tone === "red" ? "red" : "gray";
    const banner = element("section", {
      className: `status-banner status-banner--${tone}`,
      attrs: { role: "status" },
    });
    const text = element("div", { className: "status-banner-copy" }, [
      element("h2", { text: copy.label }),
      element("p", { text: copy.subtitle }),
    ]);
    banner.append(text);
    if (copy.action) {
      banner.append(
        element("div", { className: "status-banner-actions" }, [
          element("button", {
            className: "button-quiet",
            type: "button",
            text: copy.action.label,
            dataset: { action: "status-command", command: copy.action.command },
          }),
        ]),
      );
    }
    return banner;
  }

  function renderCallPanel() {
    const panel = state.callPanel || createCallPanel();
    updateCallPanel(panel);
    return panel;
  }

  function createCallPanel() {
    const panel = element("section", {
      id: "call-panel",
      className: "call-panel is-expanded",
      attrs: { "aria-label": "通話中", "data-mode": "expanded" },
    });
    const avatar = element("div", { className: "call-avatar", attrs: { "aria-hidden": "true" }, text: "◒" });
    const statusDot = element("span", { className: "status-dot", attrs: { "aria-hidden": "true" } });
    const wave = element("div", { className: "call-wave", attrs: { "aria-hidden": "true" } });
    for (let index = 0; index < 7; index += 1) {
      wave.append(element("span"));
    }
    const helpActions = element("div", { className: "call-help-actions" });
    const statusText = element("strong", { id: "call-status-text" });
    const subtitle = element("p", { className: "call-subtitle" });
    const callCopy = element("div", { className: "call-copy" }, [
      element("div", { className: "call-status-line" }, [
        statusDot,
        statusText,
      ]),
      subtitle,
      wave,
      helpActions,
    ]);
    const callTime = element("span", { id: "call-time", className: "call-time" });
    const toggle = element("button", {
      className: "icon-button",
      type: "button",
      dataset: { action: "toggle-call-panel" },
    });
    const controls = element("div", { className: "call-controls" }, [
      callTime,
      toggle,
      element("button", {
        className: "icon-button call-hangup",
        type: "button",
        text: "×",
        dataset: { action: "hangup" },
        attrs: { "aria-label": "掛斷", title: "掛斷" },
      }),
    ]);
    panel.append(avatar, callCopy, controls);
    state.callPanel = panel;
    state.callPanelParts = { statusDot, statusText, subtitle, helpActions, callTime, toggle };
    return panel;
  }

  function updateCallPanel(panel) {
    const parts = state.callPanelParts;
    if (!parts) {
      return;
    }
    const copy = statusFor(state.snapshot.app.state);
    const collapsed = state.panelMode === "collapsed";
    panel.classList.toggle("is-collapsed", collapsed);
    panel.classList.toggle("is-expanded", !collapsed);
    panel.dataset.mode = state.panelMode;
    setDotTone(parts.statusDot, copy.tone);
    parts.statusText.textContent = copy.label;
    parts.subtitle.textContent = copy.subtitle;
    parts.callTime.textContent = formatElapsed(currentElapsed());
    parts.toggle.textContent = collapsed ? "⌃" : "⌄";
    parts.toggle.setAttribute("aria-label", collapsed ? "展開通話面板" : "收合通話面板");
    parts.toggle.setAttribute("title", collapsed ? "展開通話面板" : "收合通話面板");
    parts.toggle.setAttribute("aria-expanded", String(!collapsed));
    clear(parts.helpActions);
    if (copy.action) {
      parts.helpActions.append(
        element("button", {
          className: "button-text",
          type: "button",
          text: copy.action.label,
          dataset: { action: "status-command", command: copy.action.command },
        }),
      );
    }
  }

  function releaseCallPanel() {
    if (state.callPanel) {
      state.callPanel.remove();
    }
    state.callPanel = null;
    state.callPanelParts = null;
  }

  function renderReadonlyBanner() {
    const callThread = state.snapshot.threads.find(
      (thread) => thread.id === state.snapshot.call.threadId,
    );
    const title = callThread?.title || "原本的對話";
    return element("section", { className: "readonly-banner", attrs: { role: "status" } }, [
      element("span", { text: `通話仍在「${title}」進行中；這個對話目前只能閱讀。` }),
      element("button", {
        className: "button-quiet",
        type: "button",
        text: "回到通話",
        dataset: { action: "return-to-call" },
      }),
    ]);
  }

  function renderChatEmptyState() {
    const hasThread = Boolean(state.selectedThreadId);
    return element("section", { className: "empty-state" }, [
      element("h2", { text: hasThread ? "從這裡開始聊" : "準備好和 Lune 說話了" }),
      element("p", {
        text: hasThread
          ? "你可以打字，或按右上角的「打給 Lune」。"
          : "建立一個新對話，或按右上角的「打給 Lune」。",
      }),
    ]);
  }

  function renderMessage(message) {
    const user = message.role === "user";
    const item = element("article", {
      className: `message message--${user ? "user" : "assistant"}`,
      attrs: { "aria-label": user ? "你說" : "Lune 說" },
    });
    item.append(
      element("span", { className: "message-label", text: user ? "你" : "Lune" }),
      element("div", { className: "message-bubble", text: message.text }),
    );
    const detail = element("div", { className: "message-meta" });
    const time = formatMessageTime(message.createdAt);
    if (time) {
      detail.append(document.createTextNode(time));
    }
    if (message.memoryIds.length > 0) {
      const memoryLink = element("button", {
        className: "message-memory-note",
        type: "button",
        text: "・她想起了一件事",
        dataset: { action: "show-memories", memoryId: message.memoryIds[0] },
      });
      detail.append(memoryLink);
    }
    if (detail.childNodes.length > 0) {
      item.append(detail);
    }
    return item;
  }

  function renderComposer(readonly) {
    const wrap = element("div", { className: "composer-wrap" });
    const form = element("form", {
      className: "composer",
      dataset: { form: "composer" },
      attrs: { "aria-label": "輸入訊息" },
    });
    const input = element("textarea", {
      name: "text",
      placeholder: readonly ? "這個對話正在唯讀瀏覽" : "說點什麼…",
      disabled: readonly || !state.selectedThreadId,
      dataset: { composer: "true", autoresize: "true" },
      attrs: { rows: "1", "aria-label": "輸入訊息" },
    });
    const speechButton = element("button", {
      className: "composer-toggle",
      type: "button",
      text: state.speakText ? "♬" : "♩",
      disabled: readonly || !state.selectedThreadId,
      dataset: { action: "toggle-speak-text" },
      attrs: {
        "aria-label": state.speakText ? "傳送文字會朗讀，按一下關閉" : "傳送文字不會朗讀，按一下開啟",
        "aria-pressed": String(state.speakText),
        title: state.speakText ? "傳送文字會朗讀" : "傳送文字不會朗讀",
      },
    });
    const send = element("button", {
      className: "composer-send",
      type: "submit",
      text: "↑",
      disabled: readonly || !state.selectedThreadId,
      attrs: { "aria-label": "送出訊息", title: "送出訊息" },
    });
    form.append(input, speechButton, send);
    const help = element("p", {
      className: "composer-help",
      text: state.snapshot.call.active
        ? "通話中送出文字會直接打斷她。"
        : "Enter 送出，Shift + Enter 換行。",
    });
    wrap.append(form, help);
    return wrap;
  }

  function renderMemories() {
    const view = element("section", { className: "memory-view" });
    const scroll = element("div", { className: "page-scroll" });
    const content = element("div", { className: "content-width" });
    content.append(
      pageHeading("Lune 記得的事", "這裡是她在對話之外留下的長期記憶。"),
    );
    const toolbar = element("div", { className: "memory-toolbar" });
    const shell = element("div", { className: "input-shell search-field" });
    shell.append(
      element("input", {
        type: "search",
        value: state.memoryQuery,
        placeholder: "找一件她記得的事",
        dataset: { memorySearch: "true" },
        attrs: { "aria-label": "搜尋記憶" },
      }),
    );
    toolbar.append(
      shell,
      element("p", { className: "field-hint", text: "她是照意思找，不是照字找。" }),
    );
    content.append(toolbar, renderMemoryGroups());
    content.append(
      element("p", {
        className: "no-bulk-note",
        text: "這裡沒有「全部清空」。每一筆記憶都要分開確認；若真的想從頭開始，需要自行處理資料庫檔案。",
      }),
    );
    scroll.append(content);
    view.append(scroll);
    return view;
  }

  function renderMemoryGroups() {
    const groups = element("div", { className: "memory-groups" });
    const memories = displayedMemories();
    if (memories.length === 0) {
      groups.append(
        element("section", { className: "empty-state" }, [
          element("h2", { text: state.memoryQuery ? "沒有找到接近的記憶" : "她還沒記住什麼" }),
          element("p", {
            text: state.memoryQuery
              ? "換個說法試試看；她會依意思找，而不是逐字比對。"
              : "多聊幾次之後，她會自己把重要的事記下來。你也可以直接跟她說「記住這件事」。",
          }),
        ]),
      );
      return groups;
    }
    const explicit = memories.filter((memory) => isExplicitMemory(memory.source));
    const observed = memories.filter((memory) => !isExplicitMemory(memory.source));
    groups.append(
      renderMemoryGroup("你叫她記住的", "這些是你明確交代她留下的。", explicit),
      renderMemoryGroup("她自己注意到的", "這些來自她在對話中察覺到的事。", observed),
    );
    return groups;
  }

  function displayedMemories() {
    if (!state.memoryQuery || !Array.isArray(state.searchResults)) {
      return state.snapshot.memories;
    }
    const known = new Map(state.snapshot.memories.map((memory) => [memory.id, memory]));
    return state.searchResults.map((result) => ({
      ...(known.get(result.id) || result),
      ...result,
    }));
  }

  function renderMemoryGroup(title, description, memories) {
    const group = element("section", { className: "memory-group" });
    group.append(
      element("h3", { text: title }),
      element("p", { className: "memory-group-description", text: description }),
    );
    const list = element("div", { className: "memory-list" });
    if (memories.length === 0) {
      list.append(element("p", { className: "field-hint", text: "目前沒有。" }));
    }
    for (const memory of memories) {
      list.append(renderMemoryCard(memory));
    }
    group.append(list);
    return group;
  }

  function renderMemoryCard(memory) {
    const importance = memory.importance >= 0.7 ? "high" : memory.importance >= 0.35 ? "medium" : "low";
    const detail = [];
    const created = formatDate(memory.createdAt);
    if (created) {
      detail.push(created);
    }
    if (Number.isFinite(memory.similarity)) {
      detail.push(memory.similarity >= 0.75 ? "很接近" : "有點接近");
    } else if (memory.match === "很接近" || memory.match === "有點接近") {
      detail.push(memory.match);
    }
    return element("article", { className: "memory-card" }, [
      element("span", {
        className: `memory-importance memory-importance--${importance}`,
        attrs: { "aria-label": "重要程度" },
      }),
      element("div", { className: "memory-content" }, [
        element("p", { text: memory.content }),
        detail.length ? element("small", { text: detail.join(" · ") }) : null,
      ]),
      element("button", {
        className: "button-text memory-delete",
        type: "button",
        text: "忘記",
        dataset: { action: "forget-memory", memoryId: memory.id },
        attrs: { "aria-label": "刪除這筆記憶" },
      }),
    ]);
  }

  function isExplicitMemory(source) {
    return ["user", "explicit", "user_requested", "requested", "manual"].includes(source.toLowerCase());
  }

  function renderProfile() {
    const view = element("section", { className: "profile-view" });
    const scroll = element("div", { className: "page-scroll" });
    const content = element("div", { className: "content-width" });
    content.append(pageHeading("你與 Lune 的設定檔", "一個入口，分別調整你想讓她知道的事與她說話的方式。"));
    const tabs = element("div", { className: "settings-tabs", attrs: { role: "tablist", "aria-label": "設定檔頁面" } });
    for (const tab of [
      ["you", "你"],
      ["lune", "Lune"],
    ]) {
      tabs.append(
        element("button", {
          className: "settings-tab",
          type: "button",
          text: tab[1],
          dataset: { action: "settings-tab", tab: tab[0] },
          attrs: {
            role: "tab",
            "aria-selected": String(state.activeSettingsTab === tab[0]),
          },
        }),
      );
    }
    content.append(tabs, state.activeSettingsTab === "lune" ? renderLuneProfilePanel() : renderUserProfilePanel());
    scroll.append(content);
    view.append(scroll);
    return view;
  }

  function renderUserProfilePanel() {
    const grid = element("div", { className: "profile-grid" });
    const form = element("form", { className: "form-card", dataset: { form: "user-profile" } });
    form.append(
      element("h3", { text: "你這頁" }),
      element("p", { text: "這些內容每次對話都會帶上。" }),
      element("div", { className: "form-stack" }, [
        field("她怎麼叫你", "name", state.snapshot.profile.name, "單行文字"),
        field(
          "你想讓她知道的事",
          "context",
          state.snapshot.profile.context,
          "每次對話都會帶上",
          true,
        ),
      ]),
      element("div", { className: "form-actions" }, [
        element("button", { className: "button-quiet", type: "submit", text: "儲存" }),
      ]),
    );
    const difference = element("aside", { className: "info-card profile-memory-difference" }, [
      element("h3", { text: "設定檔和記憶不一樣" }),
      element("p", {}, [
        element("strong", { text: "這頁是你親口說的" }),
        document.createTextNode(" —— 永遠會進對話，她一定看得到。"),
      ]),
      element("p", {}, [
        element("strong", { text: "那邊是她自己注意到的" }),
        document.createTextNode(" —— 聊到相關的事才會想起來。"),
      ]),
      element("button", {
        className: "button-text",
        type: "button",
        text: "看看她記得的事",
        dataset: { action: "show-memories" },
      }),
    ]);
    grid.append(form, difference);
    return grid;
  }

  function renderLuneProfilePanel() {
    const grid = element("div", { className: "profile-grid" });
    grid.append(renderPersonaForm("lune-profile", "儲存設定"), renderBoundariesCard());
    return grid;
  }

  function renderPersonaForm(formName, submitLabel) {
    const persona = state.snapshot.persona;
    const form = element("form", { className: "form-card", dataset: { form: formName } });
    form.append(
      element("h3", { text: "Lune 這頁" }),
      element("p", { text: "只開放能安全調整的結構化設定。" }),
    );
    const fields = element("div", { className: "form-stack" });
    const language = element("div");
    language.append(element("label", { className: "field-label", text: "說話的語言", attrs: { for: `${formName}-ratio` } }));
    const range = element("div", { className: "range-row" }, [
      element("span", { text: "英文多一點" }),
      element("input", {
        type: "range",
        value: String(Math.round(persona.chineseRatio)),
        name: "chinese_ratio",
        attrs: { id: `${formName}-ratio`, min: "0", max: "100", step: "1", "aria-label": "中文比例" },
      }),
      element("span", { text: "中文多一點" }),
    ]);
    language.append(range);
    fields.append(
      language,
      choiceField("主動程度", "initiative", persona.initiative, [
        ["gentle", "安靜一點"],
        ["balanced", "剛剛好"],
        ["proactive", "主動一些"],
      ]),
      choiceField("回話長度", "response_length", persona.responseLength, [
        ["short", "精簡"],
        ["normal", "自然"],
      ]),
      choiceField("她的聲音", "voice", persona.voice, [
        ["private", "私人聲線"],
        ["system", "系統合成音"],
      ]),
    );
    form.append(
      fields,
      element("div", { className: "form-actions" }, [
        element("button", { className: "button-quiet", type: "submit", text: submitLabel }),
      ]),
      element("p", {
        className: "save-note",
        text: "改完之後她會馬上不太一樣；既有 summaries 是照舊個性寫的，不會回溯重寫。",
      }),
    );
    return form;
  }

  function choiceField(label, name, selected, choices) {
    const fieldset = element("fieldset", { attrs: { style: "border: 0; padding: 0; margin: 0;" } });
    fieldset.append(element("legend", { className: "field-label", text: label }));
    const choicesElement = element("div", { className: "choice-group" });
    for (const [value, text] of choices) {
      const id = `choice-${name}-${value}`;
      const input = element("input", {
        type: "radio",
        name,
        value,
        checked: selected === value,
        attrs: { id },
      });
      choicesElement.append(
        element("label", { className: "choice-option", attrs: { for: id } }, [input, element("span", { text })]),
      );
    }
    fieldset.append(choicesElement);
    return fieldset;
  }

  function renderBoundariesCard() {
    return element("aside", { className: "info-card" }, [
      element("h3", { text: "不開放修改的事" }),
      element("ul", { className: "boundaries-list" }, [
        element("li", { text: "她不會假裝自己是人" }),
        element("li", { text: "她不會刻意讓你離不開她" }),
        element("li", { text: "不知道的事她會說不知道" }),
      ]),
      element("p", { text: "不是技術限制，是刻意的。這幾件事一鬆開，剩下的設定就沒有意義了。" }),
    ]);
  }

  function renderSettings() {
    const view = element("section", { className: "settings-view" });
    const scroll = element("div", { className: "page-scroll" });
    const content = element("div", { className: "content-width" });
    content.append(pageHeading("設定", "裝置狀態與這個測試階段的使用方式。"));
    const grid = element("div", { className: "settings-grid" });
    const device = element("section", { className: "info-card" }, [
      element("h3", { text: "音訊裝置" }),
      element("p", { text: state.snapshot.device.label }),
      element("div", { className: "form-actions" }, [
        element("button", {
          className: "button-quiet",
          type: "button",
          text: "重新檢查",
          dataset: { action: "status-command", command: "check_audio_devices" },
        }),
      ]),
    ]);
    const local = element("section", { className: "info-card" }, [
      element("h3", { text: "這個階段" }),
      element("p", { text: "對話全程只使用這台電腦上的本機模型；沒有 API key、沒有用量條，也不會連外。" }),
      element("div", { className: "form-actions" }, [
        element("button", {
          className: "button-quiet",
          type: "button",
          text: "重新檢查本機元件",
          dataset: { action: "status-command", command: "check_local_runtime" },
        }),
      ]),
    ]);
    grid.append(device, local);
    content.append(grid);
    scroll.append(content);
    view.append(scroll);
    return view;
  }

  function pageHeading(title, description) {
    return element("header", { className: "page-heading" }, [
      element("p", { className: "eyebrow", text: "Lune" }),
      element("h2", { text: title }),
      element("p", { text: description }),
    ]);
  }

  function field(labelText, name, value, hint, multiline = false) {
    const fieldElement = element("div");
    const id = `field-${name}`;
    fieldElement.append(element("label", { className: "field-label", text: labelText, attrs: { for: id } }));
    fieldElement.append(
      element(multiline ? "textarea" : "input", {
        className: "field-control",
        type: multiline ? undefined : "text",
        name,
        value,
        attrs: multiline ? { id, rows: "5" } : { id },
      }),
    );
    if (hint) {
      fieldElement.append(element("p", { className: "field-hint", text: hint }));
    }
    return fieldElement;
  }

  function renderSetup() {
    const setup = state.snapshot.setup || {};
    const currentStep = getSetupCurrentStep(setup);
    clear(refs.setupStepList);
    for (const step of setupSteps) {
      const complete = isSetupStepComplete(step.id, setup);
      const active = !complete && currentStep === step.id;
      const listItem = element("li", {
        className: `setup-step${complete ? " is-complete" : ""}${active ? " is-active" : ""}`,
      });
      // Every step stays openable.  The old gate asked for the local, model
      // and persona reasons to be clear at once, which is exactly the state
      // that ends setup: step 4 could be listed but never opened.
      const button = element("button", {
        className: "setup-step-button",
        type: "button",
        dataset: { action: "select-setup-step", setupStep: step.id },
        attrs: { "aria-current": active ? "step" : null },
      });
      button.append(
        element("span", { className: "setup-step-index", text: complete ? "✓" : step.number }),
        element("span", { className: "setup-step-copy", text: `${step.label}${step.optional ? "（選配）" : ""}` }),
      );
      const progress = setupProgressFor(step.id, setup);
      if (progress) {
        button.append(element("span", { className: "setup-step-progress", text: progress }));
      }
      listItem.append(button);
      refs.setupStepList.append(listItem);
    }
    clear(refs.setupContent);
    refs.setupContent.append(renderSetupCard(currentStep, setup));
  }

  function getSetupCurrentStep(setup) {
    const requested = state.setupStepOverride;
    if (
      requested &&
      setupSteps.some((step) => step.id === requested) &&
      !isSetupStepComplete(requested, setup)
    ) {
      return requested;
    }
    state.setupStepOverride = null;
    const reasons = new Set(asArray(setup.reasons));
    if ([...CONFIG_REASONS].some((reason) => reasons.has(reason))) {
      return "repair";
    }
    const explicit = stringValue(setup.currentStep);
    // "repair" is a blocking card rather than one of the numbered steps, so it
    // is honoured here as well as derived from the reasons above.
    if (explicit === "repair" || setupSteps.some((step) => step.id === explicit)) {
      return explicit;
    }
    return setupSteps.find((step) => !isSetupStepComplete(step.id, setup))?.id || "voice";
  }

  function isSetupStepComplete(id, setup) {
    const steps = asObject(setup.steps);
    const step = asObject(steps[id]);
    if (boolValue(firstValue(step.complete, step.completed, step.done), false)) {
      return true;
    }
    const reasons = new Set(asArray(setup.reasons));
    if (id === "local") {
      return ![...LOCAL_LLM_REASONS].some((reason) => reasons.has(reason));
    }
    if (id === "models") {
      return ![...MODEL_REASONS].some((reason) => reasons.has(reason));
    }
    if (id === "persona") {
      return ![...PERSONA_REASONS].some((reason) => reasons.has(reason));
    }
    // Steps 4 and 5 have no reason code of their own, so the only honest
    // source for them is the `complete` flag the runtime computes.
    return boolValue(firstValue(step.complete, step.completed, step.skipped), false);
  }

  function setupProgressFor(id, setup) {
    if (id !== "models") {
      return "";
    }
    const downloads = asArray(setup.downloads);
    const known = downloads.filter((item) => item.progress !== null);
    if (known.length === 0) {
      return "";
    }
    const progress = Math.round(known.reduce((sum, item) => sum + item.progress, 0) / known.length);
    return `${progress}%`;
  }

  function renderSetupCard(step, setup) {
    switch (step) {
      case "repair":
        return renderSetupRepairCard(setup);
      case "models":
        return renderSetupModelsCard(setup);
      case "persona":
        return renderSetupPersonaCard();
      case "audio":
        return renderSetupAudioCard();
      case "voice":
        return renderSetupVoiceCard(setup);
      case "local":
      default:
        return renderSetupLocalCard(setup);
    }
  }

  function setupCard(title, description) {
    return element("section", { className: "setup-card" }, [
      element("p", { className: "eyebrow", text: "設定 Lune" }),
      element("h2", { text: title }),
      element("p", { text: description }),
    ]);
  }

  function renderSetupLocalCard(setup) {
    const card = setupCard("先確認本機的語言模型", "這個測試階段，連文字都不會離開這台電腦。");
    const missing = setup.reasons.filter((reason) => LOCAL_LLM_REASONS.has(reason));
    const detail = element("ul", { className: "setup-detail-list" });
    if (missing.length === 0) {
      detail.append(element("li", {}, [element("span", { text: "✓" }), "已找到已釘選的本機模型與 worker runtime。"]));
    } else {
      for (const reason of missing) {
        detail.append(
          element("li", {}, [
            element("span", { text: "—" }),
            reason === "local_llm_model_missing"
              ? "找不到已釘選的 Qwen 本機模型。"
              : "找不到 Qwen worker runtime。",
          ]),
        );
      }
      detail.append(
        element("li", {}, [
          element("span", { text: "i" }),
          "為了可驗證與離線使用，Lune 不會代為下載；請手動放好後再檢查。",
        ]),
      );
    }
    card.append(detail, element("div", { className: "form-actions" }, [
      element("button", {
        className: "button-quiet",
        type: "button",
        text: "再檢查一次",
        dataset: { action: "status-command", command: "check_local_runtime" },
      }),
    ]));
    return card;
  }

  function renderSetupRepairCard(setup) {
    const reasons = new Set(asArray(setup.reasons));
    const invalid = reasons.has("config_invalid");
    const card = setupCard(
      invalid ? "Lune 的設定檔需要重新檢查" : "正在建立預設設定",
      invalid
        ? "這不是你少做了什麼。Lune 無法讀取一份本機設定，為了不誤改你的資料，先停在這裡。"
        : "第一次啟動會自動建立預設設定；這個階段連文字都不會離開這台電腦。",
    );
    card.append(
      element("ul", { className: "setup-detail-list" }, [
        element("li", {}, [
          element("span", { text: "i" }),
          invalid
            ? "請依安裝說明確認設定檔後再試；介面不會覆寫一份無法驗證的設定。"
            : "Lune 會使用預設值建立新的本機設定，不需要 API key。",
        ]),
      ]),
      element("div", { className: "form-actions" }, [
        element("button", {
          className: "button-quiet",
          type: "button",
          text: "再檢查一次",
          dataset: { action: "status-command", command: "check_local_runtime" },
        }),
      ]),
    );
    return card;
  }

  function renderSetupModelsCard(setup) {
    const card = setupCard("準備聽懂與記住的本機模型", "下載或驗證中的元件會在背景進行；你可以同時繼續填下一步。 ");
    const downloads = asArray(setup.downloads);
    if (downloads.length > 0) {
      const progress = element("div", { className: "download-progress" });
      for (const download of downloads) {
        const row = element("div", { className: "download-row" });
        row.append(
          element("span", { text: download.label }),
          element("span", { text: download.progress === null ? download.state || "等待中" : `${Math.round(download.progress)}%` }),
        );
        if (download.progress !== null) {
          row.append(element("progress", { attrs: { max: "100", value: String(download.progress) } }));
        }
        progress.append(row);
      }
      card.append(progress);
    } else {
      card.append(
        element("ul", { className: "setup-detail-list" }, [
          element("li", {}, [element("span", { text: "i" }), "Lune 會顯示由系統管理的本機模型準備進度。"]),
          element("li", {}, [element("span", { text: "—" }), "如果缺少模型，請依安裝說明手動放置；此介面不會下載模型。"]),
        ]),
      );
    }
    card.append(
      element("div", { className: "form-actions" }, [
        element("button", {
          className: "button-quiet",
          type: "button",
          text: "重新檢查",
          dataset: { action: "status-command", command: "check_local_runtime" },
        }),
      ]),
    );
    return card;
  }

  function renderSetupPersonaCard() {
    const card = setupCard("讓她知道怎麼陪你", "這些欄位和之後的 Lune 設定頁完全相同。 ");
    const form = renderPersonaForm("setup-persona", "儲存並繼續");
    form.classList.remove("form-card");
    card.append(form);
    return card;
  }

  function renderSetupAudioCard() {
    // Setup deliberately runs without an engine, and the macOS permission
    // prompt belongs to it, so this card explains what will happen on the
    // first call instead of offering a button that could only fail here.
    const card = setupCard(
      "讓你們好好聽見彼此",
      "冷啟動時麥克風是關的。等你按下「打給 Lune」，她才會向 macOS 要求麥克風權限。",
    );
    card.append(
      element("ul", { className: "setup-detail-list" }, [
        element("li", {}, [element("span", { text: "1" }), "先接上耳機；用內建喇叭時，她會自動暫停避免聽見自己。"]),
        element("li", {}, [element("span", { text: "2" }), "第一次通話時 macOS 會問一次麥克風權限，答應之後就不會再問。"]),
      ]),
      element("div", { className: "form-actions" }, [
        element("button", {
          className: "button-quiet",
          type: "button",
          text: "再檢查一次裝置",
          dataset: { action: "status-command", command: "check_audio_devices" },
        }),
      ]),
    );
    return card;
  }

  function renderSetupVoiceCard() {
    const card = setupCard("她的聲音，可以之後再說", "私人聲線是選配；跳過時，Lune 會先用系統合成音。 ");
    const form = element("form", { dataset: { form: "setup-voice" } });
    form.append(
      choiceField("她的聲音", "voice", state.snapshot.persona.voice, [
        ["private", "私人聲線"],
        ["system", "先用系統合成音"],
      ]),
      element("div", { className: "form-actions" }, [
        element("button", { className: "button-quiet", type: "submit", text: "儲存設定" }),
        element("button", {
          className: "button-text setup-skip-button",
          type: "button",
          text: "先跳過，使用系統合成音",
          dataset: { action: "skip-voice" },
        }),
      ]),
    );
    card.append(form);
    return card;
  }

  function statusFor(appState) {
    return STATUS_COPY[appState] || {
      label: appState === "mic_off" ? "不通話" : "Lune 準備中",
      subtitle: "",
      tone: "quiet",
    };
  }

  function setDotTone(dot, tone) {
    dot.classList.remove("status-dot--accent", "status-dot--yellow", "status-dot--red", "status-dot--quiet");
    dot.classList.add(
      tone === "accent"
        ? "status-dot--accent"
        : tone === "yellow"
          ? "status-dot--yellow"
          : tone === "red"
            ? "status-dot--red"
            : "status-dot--quiet",
    );
  }

  function isReadonlyThread() {
    const call = state.snapshot.call;
    return Boolean(
      call.active &&
        (call.readonly ||
          (call.threadId && state.selectedThreadId && call.threadId !== state.selectedThreadId)),
    );
  }

  function formatMessageTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) {
      return "";
    }
    return new Intl.DateTimeFormat("zh-TW", { hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) {
      return "";
    }
    return new Intl.DateTimeFormat("zh-TW", { month: "numeric", day: "numeric" }).format(date);
  }

  function formatElapsed(seconds) {
    const value = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(value / 60);
    const remainder = value % 60;
    return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }

  function currentElapsed() {
    if (!state.snapshot.call.active) {
      return state.snapshot.call.elapsedSeconds;
    }
    return state.timerBase + Math.floor((Date.now() - state.timerStartedAt) / 1000);
  }

  function updateTimerSource() {
    window.clearInterval(state.timer);
    state.timer = null;
    state.timerBase = state.snapshot.call.elapsedSeconds;
    state.timerStartedAt = Date.now();
    if (!state.snapshot.call.active) {
      return;
    }
    state.timer = window.setInterval(() => {
      const timer = document.getElementById("call-time");
      if (timer) {
        timer.textContent = formatElapsed(currentElapsed());
      }
    }, 1000);
  }

  function toggleCallPanel() {
    state.panelMode = state.panelMode === "collapsed" ? "expanded" : "collapsed";
    const panel = document.getElementById("call-panel");
    if (!panel) {
      return;
    }
    updateCallPanel(panel);
  }

  function collapseCallPanel() {
    if (!state.snapshot.call.active || state.panelMode === "collapsed") {
      return;
    }
    state.panelMode = "collapsed";
    const panel = document.getElementById("call-panel");
    if (panel) {
      updateCallPanel(panel);
    }
  }

  function showToast(text, kind = "") {
    clear(refs.toastRegion);
    const toast = element("div", { className: `toast${kind ? ` is-${kind}` : ""}`, text, attrs: { role: "status" } });
    refs.toastRegion.append(toast);
    window.setTimeout(() => {
      if (toast.isConnected) {
        toast.remove();
      }
    }, 4200);
  }

  function handleClick(event) {
    const actionElement = event.target.closest("[data-action]");
    if (!actionElement) {
      return;
    }
    const action = actionElement.dataset.action;
    switch (action) {
      case "select-thread":
        selectThread(actionElement.dataset.threadId);
        break;
      case "show-memories":
        state.activeView = "memories";
        state.sidebarMobileOpen = false;
        render();
        break;
      case "settings-tab":
        state.activeSettingsTab = actionElement.dataset.tab === "lune" ? "lune" : "you";
        render();
        break;
      case "toggle-call-panel":
        toggleCallPanel();
        break;
      case "hangup":
        sendCommand("set_microphone", { enabled: false });
        showToast("正在掛斷…");
        break;
      case "return-to-call":
        returnToCall();
        break;
      case "toggle-speak-text":
        state.speakText = !state.speakText;
        sendCommand("set_text_speech", { enabled: state.speakText });
        renderCurrentView();
        break;
      case "select-setup-step": {
        const step = normalizeSetupStepId(actionElement.dataset.setupStep);
        if (step && state.snapshot.setup) {
          state.setupStepOverride = step;
          renderSetup();
        }
        break;
      }
      case "forget-memory":
        openForgetDialog(actionElement.dataset.memoryId);
        break;
      case "skip-voice":
        sendCommand("set_voice", { voice: "system" });
        showToast("會先使用系統合成音。");
        break;
      case "status-command":
        runStatusCommand(actionElement.dataset.command);
        break;
      default:
        break;
    }
  }

  function selectThread(threadId) {
    if (!threadId || !state.snapshot.threads.some((thread) => thread.id === threadId)) {
      return;
    }
    const previousThreadId = state.selectedThreadId;
    state.selectedThreadId = threadId;
    state.pendingThreadSelection = threadId;
    state.activeView = "chat";
    state.sidebarMobileOpen = false;
    const sent = sendCommand(
      "select_thread",
      { thread_id: threadId },
      {
        onSuccess: () => {
          state.pendingThreadSelection = null;
        },
        onError: () => {
          if (state.pendingThreadSelection === threadId) {
            state.pendingThreadSelection = null;
            state.selectedThreadId = state.snapshot.activeThreadId || previousThreadId;
            render();
          }
        },
      },
    );
    if (!sent) {
      state.pendingThreadSelection = null;
      state.selectedThreadId = previousThreadId;
    }
    render();
  }

  function returnToCall() {
    const threadId = state.snapshot.call.threadId;
    if (threadId) {
      selectThread(threadId);
    }
    state.activeView = "chat";
    state.panelMode = "expanded";
    render();
  }

  function runStatusCommand(command) {
    if (!command) {
      return;
    }
    sendCommand(command, {});
  }

  function handleSubmit(event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.form) {
      return;
    }
    event.preventDefault();
    const data = new FormData(form);
    switch (form.dataset.form) {
      case "composer":
        submitText(form, data);
        break;
      case "user-profile":
        sendCommand("save_user_profile", {
          name: stringValue(data.get("name")).trim(),
          context: stringValue(data.get("context")).trim(),
        });
        showToast("正在儲存設定檔…");
        break;
      case "lune-profile":
      case "setup-persona":
        savePersona(data, form.dataset.form === "setup-persona");
        break;
      case "setup-voice":
        sendCommand("set_voice", { voice: stringValue(data.get("voice"), "system") });
        showToast("正在儲存聲音設定…");
        break;
      default:
        break;
    }
  }

  function submitText(form, data) {
    const text = stringValue(data.get("text")).trim();
    if (!text || !state.selectedThreadId || isReadonlyThread()) {
      return;
    }
    const sent = sendCommand("submit_text", {
      thread_id: state.selectedThreadId,
      text,
      speak: state.speakText,
    });
    if (sent) {
      const input = form.elements.namedItem("text");
      if (input instanceof HTMLTextAreaElement) {
        input.value = "";
        resizeTextarea(input);
      }
    }
  }

  function savePersona(data, setupFlow) {
    const ratio = Math.max(0, Math.min(100, numberValue(data.get("chinese_ratio"), 70))) / 100;
    sendCommand("save_persona", {
      chinese_ratio: ratio,
      initiative: stringValue(data.get("initiative"), "balanced"),
      response_length: stringValue(data.get("response_length"), "normal"),
      voice: stringValue(data.get("voice"), "system"),
      setup: setupFlow,
    });
    showToast(setupFlow ? "正在儲存，接著會重新檢查設定…" : "正在儲存 Lune 的設定…");
  }

  function handleKeydown(event) {
    const target = event.target;
    if (!(target instanceof HTMLTextAreaElement) || target.dataset.composer !== "true") {
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      const form = target.closest("form");
      if (form) {
        form.requestSubmit();
      }
    }
  }

  function handleInput(event) {
    const target = event.target;
    if (target instanceof HTMLTextAreaElement && target.dataset.autoresize === "true") {
      resizeTextarea(target);
    }
    if (target instanceof HTMLInputElement && target.dataset.memorySearch === "true") {
      state.memoryQuery = target.value;
      window.clearTimeout(state.memorySearchTimer);
      if (!state.memoryQuery.trim()) {
        state.searchResults = null;
        renderCurrentView();
        return;
      }
      state.memorySearchTimer = window.setTimeout(() => {
        sendCommand("search_memories", { query: state.memoryQuery });
      }, 250);
    }
  }

  function resizeTextarea(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
  }

  function openForgetDialog(memoryId) {
    if (!memoryId) {
      return;
    }
    state.pendingForgetId = memoryId;
    refs.confirmCopy.textContent = "這會從硬碟抹掉這筆記憶，沒有還原方式；但對話紀錄仍在。";
    if (typeof refs.confirmDialog.showModal === "function") {
      refs.confirmDialog.showModal();
    } else {
      const accepted = window.confirm(refs.confirmCopy.textContent);
      if (accepted) {
        sendCommand("forget_memory", { memory_id: memoryId, confirmation: memoryId });
      }
      state.pendingForgetId = null;
    }
  }

  function openRenameDialog() {
    const threadId = state.selectedThreadId;
    const thread = state.snapshot.threads.find((item) => item.id === threadId);
    if (!thread || isReadonlyThread()) {
      return;
    }
    state.pendingRenameThreadId = thread.id;
    refs.renameInput.value = thread.title;
    if (typeof refs.renameDialog.showModal === "function") {
      refs.renameDialog.showModal();
      queueMicrotask(() => {
        refs.renameInput.focus();
        refs.renameInput.select();
      });
      return;
    }
    const title = window.prompt("替這個對話取個名字", thread.title);
    if (title !== null && title.trim()) {
      sendCommand("rename_thread", { thread_id: thread.id, title: title.trim() });
    }
    state.pendingRenameThreadId = null;
  }

  function handleDialogClose() {
    if (refs.confirmDialog.returnValue === "confirm" && state.pendingForgetId) {
      sendCommand("forget_memory", {
        memory_id: state.pendingForgetId,
        confirmation: state.pendingForgetId,
      });
    }
    state.pendingForgetId = null;
    refs.confirmDialog.returnValue = "";
  }

  function handleRenameDialogClose() {
    const threadId = state.pendingRenameThreadId;
    const title = refs.renameInput.value.trim();
    if (refs.renameDialog.returnValue === "confirm" && threadId) {
      if (!title) {
        showToast("對話名稱不能是空白。", "error");
      } else {
        sendCommand("rename_thread", { thread_id: threadId, title });
        showToast("正在儲存對話名稱…");
      }
    }
    state.pendingRenameThreadId = null;
    refs.renameDialog.returnValue = "";
  }

  function connectAfterBootstrap(bootstrap) {
    if (!isBootstrap(bootstrap)) {
      setConnectionText("無法取得 Lune 的本機連線資訊。", "error");
      return;
    }
    state.bootstrap = bootstrap;
    openSocket();
  }

  function isBootstrap(value) {
    return (
      isObject(value) &&
      typeof value.url === "string" &&
      value.url.length > 0 &&
      typeof value.token === "string" &&
      value.token.length > 0 &&
      (typeof value.protocol === "string" || typeof value.protocol === "number")
    );
  }

  function openSocket() {
    if (!state.bootstrap || typeof window.WebSocket !== "function") {
      setConnectionText("這個介面目前無法建立本機連線。", "error");
      return;
    }
    if (state.websocket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.websocket.readyState)) {
      return;
    }
    setConnectionText(state.ready ? "正在重新連線到 Lune…" : "正在連線到 Lune…");
    let socket;
    try {
      socket = new WebSocket(state.bootstrap.url);
    } catch (_error) {
      refreshBootstrapAfterDisconnect();
      return;
    }
    state.websocket = socket;
    socket.addEventListener("open", () => {
      if (state.websocket !== socket) {
        return;
      }
      try {
        socket.send(
          JSON.stringify({
            type: "hello",
            protocol: state.bootstrap.protocol,
            token: state.bootstrap.token,
          }),
        );
      } catch (_error) {
        socket.close();
      }
    });
    socket.addEventListener("message", (event) => {
      if (state.websocket === socket) {
        receiveSocketMessage(event.data);
      }
    });
    socket.addEventListener("error", () => {
      if (state.websocket === socket) {
        setConnectionText("Lune 的本機連線暫時中斷。", "error");
      }
    });
    socket.addEventListener("close", () => {
      if (state.websocket !== socket) {
        return;
      }
      state.websocket = null;
      if (!state.ready) {
        setConnectionText("正在等待 Lune 啟動…");
      } else {
        setConnectionText("Lune 暫時離線，正在取得新的本機連線…", "error");
      }
      refreshBootstrapAfterDisconnect();
    });
  }

  function receiveSocketMessage(data) {
    let message;
    try {
      message = JSON.parse(stringValue(data));
    } catch (_error) {
      return;
    }
    const type = stringValue(message.type).toLowerCase();
    if (type === "snapshot") {
      acceptSnapshot(firstValue(message.snapshot, message.payload, message));
      return;
    }
    if (type === "event") {
      const eventName = stringValue(message.event);
      if (eventName === "error") {
        showToast("Lune 回報了一個問題，請檢查看看。", "error");
      } else {
        if (eventName === "snapshot" || eventName === "snapshot_changed") {
          state.ready = true;
          setConnectionText("", "connected");
        }
        mergeEvent(eventName, message.payload);
      }
      return;
    }
    if (type === "result" || type === "command_result") {
      const pending = resolvePending(message.id, true, message);
      // Several commands answer with a whole snapshot rather than an envelope
      // around one.  `forget_memory` is the one that matters: dropping its
      // reply would leave a deleted memory referenced on screen until the next
      // reconciling tick, so a bare snapshot is recognised by its own shape.
      const bareSnapshot = isSnapshotShape(message.result) ? message.result : null;
      if (message.snapshot || message.payload?.snapshot || message.result?.snapshot) {
        acceptSnapshot(message.snapshot || message.payload?.snapshot || message.result.snapshot);
      } else if (bareSnapshot) {
        acceptSnapshot(bareSnapshot);
      } else if (pending?.command === "search_memories" && Array.isArray(message.result?.results)) {
        state.searchResults = message.result.results.map(normalizeMemory);
        if (state.activeView === "memories") {
          renderCurrentView();
        }
      }
      return;
    }
    if (type === "error" || type === "command_error") {
      resolvePending(message.id, false, message);
      showToast("這個動作目前無法完成，請再試一次。", "error");
      return;
    }
    if (type === "hello" && (message.snapshot || message.payload)) {
      acceptSnapshot(message.snapshot || message.payload);
      return;
    }
    if (type === "hello_ack") {
      setConnectionText("正在取得 Lune 的目前狀態…");
      sendCommand("get_status", {});
    }
  }

  function acceptSnapshot(rawSnapshot) {
    // Every whole snapshot means this client is caught up, whichever channel
    // carried it.  The reply to `get_status` is the only one a motionless
    // setup screen ever gets, so leaving `ready` false here kept the "正在取得
    // Lune 的目前狀態…" capsule and `aria-busy="true"` on screen for good.
    state.ready = true;
    setConnectionText("", "connected");
    applySnapshot(rawSnapshot);
  }

  function refreshBootstrapAfterDisconnect() {
    // The native bridge deliberately hands out exactly one authenticated
    // bootstrap.  A new browser connection must come from a fresh app launch.
    setConnectionText("Lune 的連線已結束，請重新開啟 Lune。", "error");
  }

  function setConnectionText(text, kind = "") {
    refs.connection.textContent = text;
    refs.connection.className = `connection-status${kind ? ` is-${kind}` : ""}`;
  }

  function commandId() {
    if (window.crypto?.randomUUID) {
      return window.crypto.randomUUID();
    }
    return `lune-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function sendCommand(command, params, options = {}) {
    const socket = state.websocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      showToast("Lune 還沒有連上，請稍後再試。", "error");
      return null;
    }
    const id = commandId();
    state.pending.set(id, {
      command,
      onSuccess: options.onSuccess,
      onError: options.onError,
    });
    window.setTimeout(() => state.pending.delete(id), 30000);
    try {
      socket.send(JSON.stringify({ type: "command", id, command, params: asObject(params) }));
      return id;
    } catch (_error) {
      state.pending.delete(id);
      showToast("這個動作沒有送出去，請再試一次。", "error");
      return null;
    }
  }

  // Called by the native shell while the authenticated socket is still alive.
  // The one-time token is deliberately kept in this closure and never exposed.
  window.__luneShutdown = () => Boolean(sendCommand("shutdown", {}));

  function resolvePending(id, success, message) {
    const pending = state.pending.get(stringValue(id));
    if (!pending) {
      return null;
    }
    state.pending.delete(stringValue(id));
    if (success && typeof pending.onSuccess === "function") {
      pending.onSuccess(message);
    }
    if (!success && typeof pending.onError === "function") {
      pending.onError(message);
    }
    return pending;
  }

  async function obtainBootstrap() {
    if (state.bootstrap || state.websocket || state.bootstrapRequestInFlight) {
      return;
    }
    const developmentBootstrap = window.__LUNE_BOOTSTRAP__;
    if (isBootstrap(developmentBootstrap)) {
      connectAfterBootstrap(developmentBootstrap);
      return;
    }
    const api = window.pywebview?.api;
    if (api && typeof api.get_bootstrap === "function") {
      state.bootstrapRequestInFlight = true;
      try {
        const bootstrap = await api.get_bootstrap();
        if (isBootstrap(bootstrap)) {
          connectAfterBootstrap(bootstrap);
        } else if (!state.nativeBridgeReady) {
          setConnectionText("正在等待 Lune 的本機連線資訊…");
        } else {
          setConnectionText("無法取得 Lune 的本機連線資訊。", "error");
        }
      } catch (_error) {
        setConnectionText(
          state.nativeBridgeReady
            ? "無法取得 Lune 的本機連線資訊。"
            : "正在等待 Lune 的本機連線資訊…",
          state.nativeBridgeReady ? "error" : "",
        );
      } finally {
        state.bootstrapRequestInFlight = false;
        if (state.retryBootstrapWhenReady && !state.bootstrap) {
          state.retryBootstrapWhenReady = false;
          void obtainBootstrap();
        }
      }
      return;
    }
    setConnectionText("正在等待 Lune 啟動…");
  }

  function bindStaticEvents() {
    refs.sidebarToggle.addEventListener("click", () => {
      state.sidebarCollapsed = !state.sidebarCollapsed;
      renderSidebar();
    });
    refs.mobileSidebar.addEventListener("click", () => {
      state.sidebarMobileOpen = !state.sidebarMobileOpen;
      renderSidebar();
    });
    refs.newThread.addEventListener("click", () => {
      sendCommand("create_thread", {});
    });
    refs.renameThread.addEventListener("click", openRenameDialog);
    refs.callButton.addEventListener("click", () => {
      if (state.snapshot.call.active) {
        returnToCall();
        return;
      }
      const threadId = state.selectedThreadId;
      const requestId = sendCommand(
        "request_microphone_access",
        threadId ? { thread_id: threadId } : {},
        {
          onSuccess: () => {
            sendCommand("set_microphone", threadId ? { enabled: true, thread_id: threadId } : { enabled: true });
          },
        },
      );
      if (requestId) {
        showToast("準備開始通話…");
      }
    });
    refs.deviceButton.addEventListener("click", () => runStatusCommand("check_audio_devices"));
    refs.threadList.addEventListener("click", handleClick);
    refs.viewRoot.addEventListener("click", handleClick);
    refs.viewRoot.addEventListener("submit", handleSubmit);
    refs.viewRoot.addEventListener("keydown", handleKeydown);
    refs.viewRoot.addEventListener("input", handleInput);
    refs.setupStepList.addEventListener("click", handleClick);
    refs.setupContent.addEventListener("click", handleClick);
    refs.setupContent.addEventListener("submit", handleSubmit);
    document.querySelector(".sidebar-links").addEventListener("click", (event) => {
      const button = event.target.closest("[data-view]");
      if (!button) {
        return;
      }
      state.activeView = button.dataset.view;
      state.sidebarMobileOpen = false;
      render();
    });
    refs.confirmDialog.addEventListener("close", handleDialogClose);
    refs.renameDialog.addEventListener("close", handleRenameDialogClose);
    window.addEventListener(
      "pywebviewready",
      () => {
        state.nativeBridgeReady = true;
        if (state.bootstrapRequestInFlight) {
          state.retryBootstrapWhenReady = true;
          return;
        }
        void obtainBootstrap();
      },
      { once: true },
    );
    window.addEventListener("beforeunload", () => {
      window.clearInterval(state.timer);
      window.clearTimeout(state.reconnectTimer);
      if (state.websocket) {
        state.websocket.close();
      }
    });
  }

  function initialize() {
    bindStaticEvents();
    render();
    obtainBootstrap();
  }

  initialize();
})();
