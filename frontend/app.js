const dom = {
  workspace: document.querySelector('#workspace'),
  pickRepoButton: document.querySelector('#pickRepoButton'),
  fallbackRepoButton: document.querySelector('#fallbackRepoButton'),
  repoDirectoryInput: document.querySelector('#repoDirectoryInput'),
  pickCoverageButton: document.querySelector('#pickCoverageButton'),
  coverageInput: document.querySelector('#coverageInput'),
  repoStatus: document.querySelector('#repoStatus'),
  coverageStatus: document.querySelector('#coverageStatus'),
  taskSelectionSummary: document.querySelector('#taskSelectionSummary'),
  taskSearchInput: document.querySelector('#taskSearchInput'),
  taskList: document.querySelector('#taskList'),
  fileSearchInput: document.querySelector('#fileSearchInput'),
  fileTree: document.querySelector('#fileTree'),
  treemapStage: document.querySelector('#treemapStage'),
  treemapEmptyState: document.querySelector('#treemapEmptyState'),
  currentFilePath: document.querySelector('#currentFilePath'),
  currentFileMeta: document.querySelector('#currentFileMeta'),
  codeView: document.querySelector('#codeView'),
  toggleSidebarButton: document.querySelector('#toggleSidebarButton'),
  inspectorContent: document.querySelector('#inspectorContent'),
};

const numberFormatter = new Intl.NumberFormat();

const state = {
  taskTree: [],
  coverageTasks: [],
  taskNodeIndex: new Map(),
  selectedTaskIds: new Set(),
  collapsedNodeIds: new Set(),
  repoSource: null,
  currentFile: null,
  currentLine: null,
  taskSearch: '',
  fileSearch: '',
  currentFileView: null,
  fileTextCache: new Map(),
  fileStatsCache: new Map(),
  fileStatsPromises: new Map(),
  repoFilePaths: null,
  repoFilePathsPromise: null,
  repoTrackedPaths: null,
  repoTrackedPathsPromise: null,
  treemapView: null,
  treemapRenderToken: 0,
  loadToken: 0,
  sidebarVisible: true,
};

initialize();

function initialize() {
  dom.pickRepoButton.addEventListener('click', handlePickRepoClick);
  dom.fallbackRepoButton.addEventListener('click', () => dom.repoDirectoryInput.click());
  dom.repoDirectoryInput.addEventListener('change', handleRepoDirectoryInput);

  dom.pickCoverageButton.addEventListener('click', () => dom.coverageInput.click());
  dom.coverageInput.addEventListener('change', handleCoverageInput);
  dom.toggleSidebarButton.addEventListener('click', () => {
    state.sidebarVisible = !state.sidebarVisible;
    renderSidebarVisibility();
  });

  dom.taskSearchInput.addEventListener('input', (event) => {
    state.taskSearch = event.target.value.trim().toLowerCase();
    renderTaskList();
  });

  dom.fileSearchInput.addEventListener('input', (event) => {
    state.fileSearch = event.target.value.trim().toLowerCase();
    renderFileTree();
  });

  dom.taskList.addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || target.type !== 'checkbox') {
      return;
    }

    const { nodeId } = target.dataset;
    if (!nodeId) {
      return;
    }

    const task = state.taskNodeIndex.get(nodeId);
    if (!task || !task.toggleable) {
      return;
    }

    setNodeSelection(task, target.checked);
    syncAfterTaskFilterChange();
  });

  dom.taskList.addEventListener('click', (event) => {
    const target = event.target.closest('[data-disclosure-node-id]');
    if (!(target instanceof HTMLElement)) {
      return;
    }

    const { disclosureNodeId } = target.dataset;
    if (!disclosureNodeId) {
      return;
    }

    if (state.collapsedNodeIds.has(disclosureNodeId)) {
      state.collapsedNodeIds.delete(disclosureNodeId);
    } else {
      state.collapsedNodeIds.add(disclosureNodeId);
    }
    renderTaskList();
  });

  dom.fileTree.addEventListener('click', (event) => {
    const target = event.target.closest('[data-file-path]');
    if (!(target instanceof HTMLElement)) {
      return;
    }

    const filePath = target.dataset.filePath;
    if (!filePath) {
      return;
    }

    state.currentFile = filePath;
    state.currentLine = null;
    state.currentFileView = null;
    renderFileTree();
    renderCodeView();
    renderInspector();
    void loadCurrentFile();
  });

  dom.codeView.addEventListener('click', (event) => {
    const target = event.target.closest('[data-line-number]');
    if (!(target instanceof HTMLElement)) {
      return;
    }

    const lineNumber = Number(target.dataset.lineNumber);
    if (!Number.isFinite(lineNumber)) {
      return;
    }

    state.currentLine = lineNumber;
    renderCodeView();
    renderInspector();
  });

  renderAll();
}

async function handlePickRepoClick() {
  if (window.showDirectoryPicker) {
    try {
      const handle = await window.showDirectoryPicker({ mode: 'read' });
      state.repoSource = {
        kind: 'handle',
        name: handle.name,
        handle,
      };
      state.fileTextCache.clear();
      state.fileStatsCache.clear();
      state.fileStatsPromises.clear();
      state.repoFilePaths = null;
      state.repoFilePathsPromise = null;
      state.repoTrackedPaths = null;
      state.repoTrackedPathsPromise = null;
      state.currentFileView = null;
      updateRepoStatus(handle.name);
      ensureCurrentFileSelection();
      renderAll();
      void loadCurrentFile();
      return;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        return;
      }
      updateRepoStatus('Picker failed', true);
    }
  }

  dom.repoDirectoryInput.click();
}

function handleRepoDirectoryInput(event) {
  const input = event.target;
  const files = Array.from(input.files || []);
  if (!files.length) {
    return;
  }

  const firstPath = normalizePath(files[0].webkitRelativePath || files[0].name);
  const rootName = firstPath.includes('/') ? firstPath.split('/')[0] : 'repo';
  const fileMap = new Map();

  for (const file of files) {
    const relativePath = normalizePath(file.webkitRelativePath || file.name);
    const strippedPath = relativePath.startsWith(`${rootName}/`)
      ? relativePath.slice(rootName.length + 1)
      : relativePath;
    fileMap.set(strippedPath, file);
  }

  state.repoSource = {
    kind: 'file-map',
    name: rootName,
    files: fileMap,
  };
  state.fileTextCache.clear();
  state.fileStatsCache.clear();
  state.fileStatsPromises.clear();
  state.repoFilePaths = null;
  state.repoFilePathsPromise = null;
  state.repoTrackedPaths = null;
  state.repoTrackedPathsPromise = null;
  state.currentFileView = null;
  updateRepoStatus(`${rootName} • ${numberFormatter.format(fileMap.size)} files`);
  ensureCurrentFileSelection();
  renderAll();
  void loadCurrentFile();
}

async function handleCoverageInput(event) {
  const input = event.target;
  const file = input.files && input.files[0];
  if (!file) {
    return;
  }

  try {
    const text = await file.text();
    const rawPayload = JSON.parse(text);
    const normalizedCoverage = normalizeCoveragePayload(rawPayload);
    const coverageModel = buildCoverageModel(normalizedCoverage);
    state.taskTree = coverageModel.taskTree;
    state.coverageTasks = coverageModel.coverageTasks;
    state.taskNodeIndex = coverageModel.taskNodeIndex;
    state.collapsedNodeIds = new Set();
    state.selectedTaskIds = new Set(state.coverageTasks.map((task) => task.id));
    state.currentLine = null;
    state.currentFileView = null;
    state.fileStatsCache.clear();
    state.fileStatsPromises.clear();
    ensureCurrentFileSelection();
    updateCoverageStatus(
      `${file.name} • ${numberFormatter.format(state.coverageTasks.length)} items`,
    );
    renderAll();
    void loadCurrentFile();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    updateCoverageStatus(`Invalid coverage JSON: ${message}`, true);
  }
}

function normalizeCoveragePayload(payload) {
  if (Array.isArray(payload)) {
    return {
      tasks: [
        {
          kind: 'task',
          prompt: 'coverage',
          coverage: payload,
          children: [],
        },
      ],
    };
  }

  if (payload && typeof payload === 'object') {
    if (Array.isArray(payload.tasks)) {
      return { tasks: payload.tasks.map(normalizeCoverageNode) };
    }

    return {
      tasks: Object.entries(payload).map(([label, rawEntries]) => {
        const descriptor = splitTaskLabel(label);
        return normalizeCoverageNode({
          kind: descriptor.kind,
          prompt: descriptor.prompt,
          coverage: Array.isArray(rawEntries) ? rawEntries : [],
          children: [],
        });
      }),
    };
  }

  throw new Error('Expected a hierarchical coverage object, a legacy task map, or a legacy array.');
}

function normalizeCoverageNode(rawNode) {
  const kind = normalizeTaskKind(rawNode?.kind);
  const prompt = typeof rawNode?.prompt === 'string' ? rawNode.prompt : '';
  const status = normalizeNodeStatus(rawNode?.status);
  const synthetic = Boolean(rawNode?.synthetic);
  const coverage = Array.isArray(rawNode?.coverage)
    ? rawNode.coverage
    : Array.isArray(rawNode?.entries)
      ? rawNode.entries
      : [];

  const rawChildren = Array.isArray(rawNode?.children)
    ? rawNode.children
    : Array.isArray(rawNode?.subagents)
      ? rawNode.subagents
      : [];
  const children = rawChildren.map(normalizeCoverageNode);

  const checklistItems = normalizeLegacyChecklist(rawNode?.checklist);
  if (checklistItems.length) {
    children.push({
      kind: 'checklist',
      prompt: 'Checklist',
      status: '',
      coverage: [],
      children: checklistItems.map((item) => ({
        kind: 'checklist_item',
        prompt: item.step,
        status: item.status,
        coverage: [],
        children: [],
      })),
    });
  }

  const thinkingSummary = normalizeLegacyText(
    rawNode?.thinking_summary ?? rawNode?.thinkingSummary,
  );
  if (thinkingSummary) {
    children.push({
      kind: 'thinking',
      prompt: thinkingSummary,
      status: '',
      coverage: [],
      children: [],
    });
  }

  return {
    kind,
    prompt,
    status,
    synthetic,
    coverage,
    children,
  };
}

function buildCoverageModel(normalizedCoverage) {
  const coverageTasks = [];
  const taskNodeIndex = new Map();
  let nextTaskId = 0;

  function entriesToFiles(coverageEntries) {
    const files = new Map();

    for (const coverageEntry of coverageEntries) {
      const cmd = typeof coverageEntry?.cmd === 'string' ? coverageEntry.cmd : '';
      const ranges = Array.isArray(coverageEntry?.ranges) ? coverageEntry.ranges : [];

      for (const rangeSpec of ranges) {
        const parsedRange = parseRangeSpec(rangeSpec);
        if (!parsedRange) {
          continue;
        }

        const existingFile = files.get(parsedRange.path) || {
          path: parsedRange.path,
          intervals: [],
        };
        existingFile.intervals.push({
          ...parsedRange,
          cmd,
        });
        files.set(parsedRange.path, existingFile);
      }
    }

    return finalizeFiles(files);
  }

  function cloneFiles(files) {
    const cloned = new Map();
    for (const [path, fileInfo] of files.entries()) {
      cloned.set(path, {
        path: fileInfo.path,
        intervals: fileInfo.intervals.map((interval) => ({ ...interval })),
        coveredLineCount: fileInfo.coveredLineCount || 0,
        commandCount: fileInfo.commandCount || 0,
      });
    }
    return cloned;
  }

  function mergeFiles(targetFiles, sourceFiles) {
    for (const [path, sourceFile] of sourceFiles.entries()) {
      const targetFile = targetFiles.get(path) || {
        path,
        intervals: [],
      };
      targetFile.intervals.push(...sourceFile.intervals.map((interval) => ({ ...interval })));
      targetFiles.set(path, targetFile);
    }
    return targetFiles;
  }

  function finalizeFiles(files) {
    for (const fileInfo of files.values()) {
      fileInfo.coveredLineCount = countUniqueLines(fileInfo.intervals);
      fileInfo.commandCount = new Set(fileInfo.intervals.map((interval) => interval.cmd)).size;
    }
    return files;
  }

  function buildCoverageTask(rawNode, ancestry = []) {
    const taskId = `task-${nextTaskId++}`;
    const kind = normalizeTaskKind(rawNode?.kind);
    const prompt = typeof rawNode?.prompt === 'string' ? rawNode.prompt : '';
    const status = normalizeNodeStatus(rawNode?.status);
    const synthetic = Boolean(rawNode?.synthetic);
    const label = formatTaskLabel(kind, prompt, status);
    const lineage = [...ancestry, { kind, prompt, status, label, synthetic }];

    const rawCoverageEntries = Array.isArray(rawNode?.coverage)
      ? rawNode.coverage
      : Array.isArray(rawNode?.entries)
        ? rawNode.entries
        : [];

    const childNodes = [];
    const rawChildren = Array.isArray(rawNode?.children)
      ? rawNode.children
      : Array.isArray(rawNode?.subagents)
        ? rawNode.subagents
        : [];

    for (const rawChild of rawChildren) {
      childNodes.push(buildCoverageTask(rawChild, lineage));
    }

    if (kind !== 'command') {
      for (const coverageEntry of rawCoverageEntries) {
        const commandPrompt = typeof coverageEntry?.cmd === 'string' && coverageEntry.cmd
          ? coverageEntry.cmd
          : 'command';
        childNodes.push(buildCoverageTask({
          kind: 'command',
          prompt: commandPrompt,
          coverage: [coverageEntry],
          children: [],
        }, lineage));
      }
    }

    const ownEntries = kind === 'command' ? rawCoverageEntries : [];
    const ownFiles = entriesToFiles(ownEntries);
    const aggregateFiles = cloneFiles(ownFiles);
    for (const child of childNodes) {
      mergeFiles(aggregateFiles, child.files);
    }
    finalizeFiles(aggregateFiles);

    let totalCoveredLines = 0;
    for (const fileInfo of aggregateFiles.values()) {
      totalCoveredLines += fileInfo.coveredLineCount || 0;
    }

    const commandCount = kind === 'command'
      ? ownEntries.length || (prompt ? 1 : 0)
      : childNodes.reduce((sum, child) => sum + child.commandCount, 0);

    const ownerPath = lineage
      .filter((entry) => entry.kind !== 'command')
      .map((entry) => entry.label)
      .join(' / ');
    const fullLineageLabels = lineage.map((entry) => entry.label);
    const fullPath = lineage.map((entry) => entry.label).join(' / ');
    const ownerLineage = lineage.filter((entry) => entry.kind !== 'command');
    const ownerKinds = ownerLineage.map((entry) => entry.kind);
    const leafTaskIds = kind === 'command'
      ? [taskId]
      : childNodes.flatMap((child) => child.leafTaskIds);
    const isCoverageLeaf = kind === 'command';

    const task = {
      id: taskId,
      label,
      kind,
      prompt,
      status,
      synthetic,
      children: childNodes,
      entries: ownEntries,
      files: aggregateFiles,
      ownFiles,
      fileCount: aggregateFiles.size,
      commandCount,
      totalCoveredLines,
      selectable: isCoverageLeaf,
      toggleable: leafTaskIds.length > 0,
      leafTaskIds,
      ownerPath,
      fullLineageLabels,
      fullPath,
      ownerLineage,
      ownerKinds,
    };

    taskNodeIndex.set(task.id, task);

    if (task.selectable) {
      coverageTasks.push(task);
    }

    return task;
  }

  return {
    taskTree: (Array.isArray(normalizedCoverage.tasks) ? normalizedCoverage.tasks : [])
      .map((rawNode) => buildCoverageTask(rawNode)),
    coverageTasks,
    taskNodeIndex,
  };
}

function setNodeSelection(task, selected) {
  if (!task || !Array.isArray(task.leafTaskIds) || !task.leafTaskIds.length) {
    return;
  }

  for (const leafTaskId of task.leafTaskIds) {
    if (selected) {
      state.selectedTaskIds.add(leafTaskId);
    } else {
      state.selectedTaskIds.delete(leafTaskId);
    }
  }
}

function getNodeSelectionState(task) {
  const leafTaskIds = Array.isArray(task?.leafTaskIds) ? task.leafTaskIds : [];
  const totalCount = leafTaskIds.length;
  let selectedCount = 0;

  for (const leafTaskId of leafTaskIds) {
    if (state.selectedTaskIds.has(leafTaskId)) {
      selectedCount += 1;
    }
  }

  return {
    totalCount,
    selectedCount,
    checked: totalCount > 0 && selectedCount === totalCount,
    partial: selectedCount > 0 && selectedCount < totalCount,
  };
}

function syncTaskCheckboxStates() {
  for (const input of dom.taskList.querySelectorAll('input[type="checkbox"][data-node-id]')) {
    if (!(input instanceof HTMLInputElement)) {
      continue;
    }

    const task = state.taskNodeIndex.get(input.dataset.nodeId || '');
    const selectionState = getNodeSelectionState(task);
    input.checked = selectionState.checked;
    input.indeterminate = selectionState.partial;
    input.setAttribute(
      'aria-checked',
      selectionState.partial ? 'mixed' : String(selectionState.checked),
    );
  }
}

function normalizeTaskKind(kind) {
  if (
    kind === 'user'
    || kind === 'subagent'
    || kind === 'commentary'
    || kind === 'checklist'
    || kind === 'checklist_item'
    || kind === 'thinking'
    || kind === 'command'
  ) {
    return kind;
  }
  return 'task';
}

function formatTaskLabel(kind, prompt, status = '') {
  if (kind === 'checklist_item' && status) {
    return prompt ? `${status} ${prompt}` : status;
  }
  return prompt ? `${kind} ${prompt}` : kind;
}

function normalizeLegacyChecklist(checklist) {
  if (!Array.isArray(checklist)) {
    return [];
  }

  return checklist
    .map((item) => {
      if (!item || typeof item !== 'object') {
        return null;
      }
      const step = typeof item.step === 'string' ? item.step.trim() : '';
      const status = typeof item.status === 'string' ? item.status.trim() : 'pending';
      if (!step) {
        return null;
      }
      return { step, status };
    })
    .filter(Boolean);
}

function normalizeLegacyText(summary) {
  if (typeof summary !== 'string') {
    return '';
  }
  return summary.trim();
}

function normalizeNodeStatus(status) {
  if (typeof status !== 'string') {
    return '';
  }
  return status.trim();
}

function splitTaskLabel(label) {
  if (label.startsWith('user ')) {
    return { kind: 'user', prompt: label.slice(5) };
  }
  if (label.startsWith('subagent ')) {
    return { kind: 'subagent', prompt: label.slice(9) };
  }
  return { kind: 'task', prompt: label };
}

function parseRangeSpec(rangeSpec) {
  if (typeof rangeSpec !== 'string') {
    return null;
  }

  const lastColon = rangeSpec.lastIndexOf(':');
  if (lastColon === -1) {
    return null;
  }
  const secondLastColon = rangeSpec.lastIndexOf(':', lastColon - 1);
  if (secondLastColon === -1) {
    return null;
  }

  const path = normalizePath(rangeSpec.slice(0, secondLastColon));
  const start = Number(rangeSpec.slice(secondLastColon + 1, lastColon));
  const end = Number(rangeSpec.slice(lastColon + 1));

  if (!path || !Number.isFinite(start) || !Number.isFinite(end)) {
    return null;
  }

  return {
    path,
    start: Math.max(1, Math.floor(start)),
    end: Math.max(1, Math.floor(end)),
  };
}

function normalizePath(path) {
  return String(path || '')
    .replace(/\\/g, '/')
    .replace(/^\.\/+/, '')
    .replace(/^\/+/, '')
    .replace(/\/+/g, '/')
    .trim();
}

function countUniqueLines(intervals) {
  if (!intervals.length) {
    return 0;
  }

  const sorted = intervals
    .map((interval) => ({
      start: Math.min(interval.start, interval.end),
      end: Math.max(interval.start, interval.end),
    }))
    .sort((left, right) => left.start - right.start || left.end - right.end);

  let covered = 0;
  let currentStart = sorted[0].start;
  let currentEnd = sorted[0].end;

  for (let index = 1; index < sorted.length; index += 1) {
    const interval = sorted[index];
    if (interval.start <= currentEnd + 1) {
      currentEnd = Math.max(currentEnd, interval.end);
      continue;
    }
    covered += currentEnd - currentStart + 1;
    currentStart = interval.start;
    currentEnd = interval.end;
  }

  covered += currentEnd - currentStart + 1;
  return covered;
}

function getSelectedTasks() {
  return state.coverageTasks.filter((task) => state.selectedTaskIds.has(task.id));
}

function buildActiveFileSummaries() {
  const summaryMap = new Map();

  for (const task of getSelectedTasks()) {
    for (const [path, fileInfo] of task.files.entries()) {
      const summary = summaryMap.get(path) || {
        path,
        intervals: [],
        taskIds: new Set(),
        commands: new Set(),
      };

      summary.taskIds.add(task.id);
      for (const interval of fileInfo.intervals) {
        summary.intervals.push(interval);
        summary.commands.add(interval.cmd);
      }
      summaryMap.set(path, summary);
    }
  }

  return Array.from(summaryMap.values())
    .map((summary) => ({
      path: summary.path,
      coveredLineCount: countUniqueLines(summary.intervals),
      taskCount: summary.taskIds.size,
      commandCount: summary.commands.size,
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
}

function getActiveFileSummaries() {
  return buildActiveFileSummaries();
}

function getCurrentFileSummary() {
  return getActiveFileSummaries().find((file) => file.path === state.currentFile) || null;
}

function ensureCurrentFileSelection() {
  const activeFileSummaries = getActiveFileSummaries();
  if (!activeFileSummaries.length) {
    state.currentFile = null;
    state.currentLine = null;
    state.currentFileView = null;
    return;
  }

  const activePaths = new Set(activeFileSummaries.map((file) => file.path));
  if (!state.currentFile || !activePaths.has(state.currentFile)) {
    state.currentFile = activeFileSummaries[0].path;
    state.currentLine = null;
    state.currentFileView = null;
  }
}

function syncAfterTaskFilterChange() {
  ensureCurrentFileSelection();
  state.currentFileView = null;
  renderAll();
  void loadCurrentFile();
}

async function loadCurrentFile() {
  if (!state.currentFile) {
    state.currentFileView = null;
    renderCodeView();
    renderInspector();
    return;
  }

  if (!state.repoSource) {
    state.currentFileView = {
      path: state.currentFile,
      missing: true,
      error: 'Load repo to open files.',
      lines: [],
      lineMeta: [],
    };
    renderCodeView();
    renderInspector();
    return;
  }

  const loadToken = ++state.loadToken;
  state.currentFileView = null;
  renderCodeView();

  try {
    const text = await readFileTextFromRepo(state.currentFile);
    if (loadToken !== state.loadToken) {
      return;
    }

    const lines = text.replace(/\r\n/g, '\n').split('\n');
    const lineMeta = buildLineMeta(state.currentFile, lines.length);
    state.currentFileView = {
      path: state.currentFile,
      missing: false,
      lines,
      lineMeta,
    };
  } catch (error) {
    if (loadToken !== state.loadToken) {
      return;
    }

    const message = error instanceof Error ? error.message : String(error);
    state.currentFileView = {
      path: state.currentFile,
      missing: true,
      error: message,
      lines: [],
      lineMeta: [],
    };
  }

  renderCodeView();
  renderInspector();
}

function buildLineMeta(path, lineCount) {
  const lineMeta = Array.from({ length: lineCount + 1 }, () => ({
    selectedTaskIds: new Set(),
    allTaskIds: new Set(),
  }));

  for (const task of state.coverageTasks) {
    const fileInfo = task.files.get(path);
    if (!fileInfo) {
      continue;
    }

    for (const interval of fileInfo.intervals) {
      const start = Math.max(1, Math.min(interval.start, interval.end));
      const end = Math.min(lineCount, Math.max(interval.start, interval.end));
      if (end < start) {
        continue;
      }

      for (let line = start; line <= end; line += 1) {
        lineMeta[line].allTaskIds.add(task.id);
        if (state.selectedTaskIds.has(task.id)) {
          lineMeta[line].selectedTaskIds.add(task.id);
        }
      }
    }
  }

  return lineMeta;
}

async function readFileTextFromRepo(path) {
  if (state.fileTextCache.has(path)) {
    return state.fileTextCache.get(path);
  }

  if (!state.repoSource) {
    throw new Error('Repository not loaded.');
  }

  let fileText = '';
  if (state.repoSource.kind === 'file-map') {
    const file = state.repoSource.files.get(path);
    if (!file) {
      throw new Error(`File not found in uploaded repo: ${path}`);
    }
    fileText = await file.text();
  } else {
    const file = await getFileFromHandle(state.repoSource.handle, path);
    fileText = await file.text();
  }

  state.fileTextCache.set(path, fileText);
  return fileText;
}

async function readFileBytesFromRepo(path) {
  if (!state.repoSource) {
    throw new Error('Repository not loaded.');
  }

  if (state.repoSource.kind === 'file-map') {
    const file = state.repoSource.files.get(path);
    if (!file) {
      throw new Error(`File not found in uploaded repo: ${path}`);
    }
    return file.arrayBuffer();
  }

  const file = await getFileFromHandle(state.repoSource.handle, path);
  return file.arrayBuffer();
}

async function loadFileStatsForPaths(paths) {
  if (!state.repoSource) {
    return;
  }

  const uniquePaths = [...new Set(paths.map((path) => normalizePath(path)).filter(Boolean))];
  if (!uniquePaths.length) {
    return;
  }

  const needsStats = uniquePaths.some((path) => !state.fileStatsCache.has(path));
  if (!needsStats) {
    return;
  }

  const loads = uniquePaths.map((path) => {
    if (state.fileStatsCache.has(path)) {
      return Promise.resolve();
    }

    if (state.fileStatsPromises.has(path)) {
      return state.fileStatsPromises.get(path);
    }

    const loadPromise = (async () => {
      try {
        const text = await readFileTextFromRepo(path);
        const totalLines = text.replace(/\r\n/g, '\n').split('\n').length;
        state.fileStatsCache.set(path, { totalLines });
      } catch (error) {
        state.fileStatsCache.set(path, { totalLines: 0 });
      } finally {
        state.fileStatsPromises.delete(path);
      }
    })();

    state.fileStatsPromises.set(path, loadPromise);
    return loadPromise;
  });

  await Promise.all(loads);

  renderFileTree();
}

async function getFileFromHandle(rootHandle, path) {
  const parts = normalizePath(path).split('/').filter(Boolean);
  let currentHandle = rootHandle;

  for (let index = 0; index < parts.length - 1; index += 1) {
    currentHandle = await currentHandle.getDirectoryHandle(parts[index]);
  }

  const fileHandle = await currentHandle.getFileHandle(parts[parts.length - 1]);
  return fileHandle.getFile();
}

function collectFileTaskDetails(path) {
  const selected = [];
  const hidden = [];

  for (const task of state.coverageTasks) {
    const fileInfo = task.files.get(path);
    if (!fileInfo) {
      continue;
    }

    const commandGroups = groupIntervalsByCommand(fileInfo.intervals);
    const detail = {
      task,
      coveredLineCount: fileInfo.coveredLineCount || countUniqueLines(fileInfo.intervals),
      commandGroups,
    };

    if (state.selectedTaskIds.has(task.id)) {
      selected.push(detail);
    } else {
      hidden.push(detail);
    }
  }

  return {
    selected: sortTaskDetails(selected),
    hidden: sortTaskDetails(hidden),
  };
}

function collectLineTaskDetails(path, lineNumber) {
  const selected = [];
  const hidden = [];

  for (const task of state.coverageTasks) {
    const fileInfo = task.files.get(path);
    if (!fileInfo) {
      continue;
    }

    const matchingIntervals = fileInfo.intervals.filter(
      (interval) => lineNumber >= interval.start && lineNumber <= interval.end,
    );
    if (!matchingIntervals.length) {
      continue;
    }

    const commandGroups = groupIntervalsByCommand(matchingIntervals);
    const detail = {
      task,
      coveredLineCount: matchingIntervals.length,
      commandGroups,
    };

    if (state.selectedTaskIds.has(task.id)) {
      selected.push(detail);
    } else {
      hidden.push(detail);
    }
  }

  return {
    selected: sortTaskDetails(selected),
    hidden: sortTaskDetails(hidden),
  };
}

function sortTaskDetails(details) {
  return details.sort((left, right) => {
    const leftKindOrder = taskKindSortValue(primaryOwnerKind(left.task));
    const rightKindOrder = taskKindSortValue(primaryOwnerKind(right.task));
    if (leftKindOrder !== rightKindOrder) {
      return leftKindOrder - rightKindOrder;
    }
    const leftLabel = left.task.fullPath || left.task.ownerPath || left.task.prompt;
    const rightLabel = right.task.fullPath || right.task.ownerPath || right.task.prompt;
    return leftLabel.localeCompare(rightLabel);
  });
}

function primaryOwnerKind(task) {
  if (commandBelongsToRealSubagent(task)) {
    return 'subagent';
  }
  if (commandBelongsToKind(task, 'user')) {
    return 'user';
  }
  return task.kind;
}

function commandBelongsToRealSubagent(task) {
  return Array.isArray(task.ownerLineage)
    && task.ownerLineage.some((entry) => entry.kind === 'subagent' && !entry.synthetic);
}

function commandBelongsToKind(task, kind) {
  return Array.isArray(task.ownerKinds) && task.ownerKinds.includes(kind);
}

function taskKindSortValue(kind) {
  if (kind === 'user') {
    return 0;
  }
  if (kind === 'subagent') {
    return 1;
  }
  return 2;
}

function groupIntervalsByCommand(intervals) {
  const commandMap = new Map();

  for (const interval of intervals) {
    const existingGroup = commandMap.get(interval.cmd) || {
      cmd: interval.cmd,
      ranges: [],
    };
    existingGroup.ranges.push(interval);
    commandMap.set(interval.cmd, existingGroup);
  }

  return Array.from(commandMap.values()).sort((left, right) => left.cmd.localeCompare(right.cmd));
}

function renderAll() {
  renderSidebarVisibility();
  void renderTreemap();
  renderTaskList();
  renderFileTree();
  renderCodeView();
  renderInspector();
}

function renderSidebarVisibility() {
  dom.workspace.classList.toggle('sidebar-hidden', !state.sidebarVisible);
  dom.toggleSidebarButton.textContent = state.sidebarVisible ? 'Hide Detail' : 'Show Detail';
  dom.toggleSidebarButton.setAttribute('aria-expanded', String(state.sidebarVisible));
}

function renderTaskList() {
  const selectedCount = state.selectedTaskIds.size;
  const totalCount = state.coverageTasks.length;
  if (!totalCount) {
    dom.taskSelectionSummary.textContent = 'No coverage';
  } else {
    dom.taskSelectionSummary.textContent = `${numberFormatter.format(selectedCount)} / ${numberFormatter.format(totalCount)}`;
  }

  if (!state.taskTree.length) {
    dom.taskList.className = 'task-list empty-state';
    dom.taskList.textContent = 'No tasks';
    return;
  }

  const query = state.taskSearch;
  const taskMarkup = renderTaskNodes(state.taskTree, query);

  dom.taskList.className = 'task-list';
  dom.taskList.innerHTML = taskMarkup || '<div class="empty-state">No match</div>';
  syncTaskCheckboxStates();
}

function renderTaskNodes(taskNodes, query, depth = 0) {
  return taskNodes
    .map((task) => renderTaskNode(task, query, depth))
    .filter(Boolean)
    .join('');
}

function renderTaskNode(task, query, depth) {
  const selectionState = getNodeSelectionState(task);
  const hasChildren = task.children.length > 0;
  const isCollapsed = !query && hasChildren && state.collapsedNodeIds.has(task.id);
  const childMarkup = renderTaskNodes(task.children, query, depth + 1);
  const matchesQuery = taskMatchesSearch(task, query);
  if (query && !matchesQuery && !childMarkup) {
    return '';
  }

  const checked = selectionState.checked ? 'checked' : '';
  const depthClass = `task-depth-${Math.min(depth, 4)}`;
  const promptClass = task.kind === 'command' ? 'task-prompt task-prompt-command' : 'task-prompt';
  const statusMarkup = task.status
    ? `<span class="checklist-status status-${escapeAttribute(task.status.toLowerCase().replace(/[^a-z0-9]+/g, '-'))}">${escapeHtml(task.status)}</span>`
    : '';
  const selectionMarkup = task.toggleable
    ? `<input type="checkbox" data-node-id="${escapeAttribute(task.id)}" ${checked}>`
    : '<span class="task-branch-marker" aria-hidden="true"></span>';
  const disclosureMarkup = hasChildren
    ? `<button type="button" class="task-disclosure" data-disclosure-node-id="${escapeAttribute(task.id)}" aria-expanded="${String(!isCollapsed)}" aria-label="${isCollapsed ? 'Expand task branch' : 'Collapse task branch'}">${isCollapsed ? '+' : '-'}</button>`
    : '<span class="task-disclosure-spacer" aria-hidden="true"></span>';
  const rowClasses = [
    'task-row',
    task.selectable ? '' : 'task-row-branch',
    task.toggleable ? '' : 'task-row-static',
    selectionState.checked || selectionState.partial ? 'active' : '',
    selectionState.partial ? 'partial' : '',
  ]
    .filter(Boolean)
    .join(' ');
  const metricMarkup = renderTaskMetrics(task, selectionState);
  const promptMarkup = task.prompt
    ? `<p class="${promptClass}" title="${escapeAttribute(task.prompt)}">${escapeHtml(task.prompt)}</p>`
    : '';
  const visibleChildrenMarkup = childMarkup && !isCollapsed
    ? `<div class="task-children">${childMarkup}</div>`
    : '';

  return `
    <div class="task-node ${depthClass}">
      <div class="${rowClasses}">
        <div class="task-row-shell">
          ${disclosureMarkup}
          <label class="task-row-label">
            ${selectionMarkup}
            <div class="task-row-body">
              <div class="task-row-meta">
                <span class="task-kind">${escapeHtml(task.kind)}</span>
                ${statusMarkup}
                ${metricMarkup}
              </div>
              ${promptMarkup}
            </div>
          </label>
        </div>
      </div>
      ${visibleChildrenMarkup}
    </div>
  `;
}

function renderTaskMetrics(task, selectionState) {
  const badges = [];

  if (task.toggleable) {
    if (!task.selectable && selectionState.totalCount > 0) {
      badges.push(
        `<span class="count-badge">${numberFormatter.format(selectionState.selectedCount)}/${numberFormatter.format(selectionState.totalCount)}</span>`,
      );
    }
    if (task.totalCoveredLines) {
      badges.push(`<span class="count-badge">${numberFormatter.format(task.totalCoveredLines)} lines</span>`);
    }
    if (!task.selectable && task.commandCount) {
      badges.push(`<span class="count-badge">${numberFormatter.format(task.commandCount)} items</span>`);
    }
  }

  return badges.join('');
}

function taskMatchesSearch(task, query) {
  if (!query) {
    return true;
  }

  const haystack = `${task.kind} ${task.status} ${task.prompt} ${task.ownerPath}`.toLowerCase();
  return haystack.includes(query);
}

function renderFileTree() {
  const activeFileSummaries = getActiveFileSummaries();
  if (!activeFileSummaries.length) {
    dom.fileTree.className = 'file-tree empty-state';
    dom.fileTree.textContent = state.coverageTasks.length
      ? 'No files'
      : 'Load repo + coverage';
    return;
  }

  const query = state.fileSearch;
  const filteredFiles = activeFileSummaries.filter((file) => {
    if (!query) {
      return true;
    }
    return file.path.toLowerCase().includes(query);
  });

  if (!filteredFiles.length) {
    dom.fileTree.className = 'file-tree empty-state';
    dom.fileTree.textContent = 'No match';
    return;
  }

  const tree = buildTree(filteredFiles);
  dom.fileTree.className = 'file-tree';
  dom.fileTree.innerHTML = renderTreeDirectory(tree, true);
  void loadFileStatsForPaths(filteredFiles.map((file) => file.path));
}

async function renderTreemap() {
  const renderToken = ++state.treemapRenderToken;
  if (!window.Plotly) {
    await clearTreemapView();
    showTreemapMessage('Plotly unavailable');
    return;
  }

  try {
    if (!state.repoSource || !state.coverageTasks.length) {
      await clearTreemapView();
      if (renderToken !== state.treemapRenderToken) {
        return;
      }
      showTreemapMessage('Load repo + coverage');
      return;
    }

    const repoFilePaths = await ensureRepoFilePaths();
    if (renderToken !== state.treemapRenderToken) {
      return;
    }

    if (!repoFilePaths.length) {
      await clearTreemapView();
      if (renderToken !== state.treemapRenderToken) {
        return;
      }
      showTreemapMessage('No repo files');
      return;
    }

    const activeFileSummaries = getActiveFileSummaries();
    await loadFileStatsForPaths(activeFileSummaries.map((file) => file.path));
    if (renderToken !== state.treemapRenderToken) {
      return;
    }

    const treemapData = buildTreemapData(
      repoFilePaths,
      activeFileSummaries,
      state.fileStatsCache,
      state.currentFile,
    );
    const trace = buildTreemapTrace(treemapData);
    const layout = buildTreemapLayout();

    await clearTreemapView();
    if (renderToken !== state.treemapRenderToken) {
      return;
    }

    const mountNode = document.createElement('div');
    mountNode.className = 'treemap-host';
    dom.treemapStage.appendChild(mountNode);
    dom.treemapEmptyState.hidden = true;

    await window.Plotly.newPlot(mountNode, [trace], layout, {
      displayModeBar: false,
      responsive: true,
    });

    if (renderToken !== state.treemapRenderToken) {
      window.Plotly.purge(mountNode);
      return;
    }

    state.treemapView = mountNode;
    mountNode.on('plotly_click', (event) => {
      const point = event?.points?.[0];
      if (!point) {
        return;
      }

      const nodeId = typeof point.id === 'string' ? point.id : '';
      const isFile = Array.isArray(point.customdata) && point.customdata[3] === 'file';
      const nextPath = resolveTreemapSelection(nodeId, isFile);
      if (!nextPath) {
        return;
      }

      state.currentFile = nextPath;
      state.currentLine = null;
      state.currentFileView = null;
      renderFileTree();
      renderCodeView();
      renderInspector();
      void loadCurrentFile();
    });
  } catch (error) {
    if (renderToken !== state.treemapRenderToken) {
      return;
    }
    await clearTreemapView();
    showTreemapMessage(`Treemap error: ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function clearTreemapView() {
  if (state.treemapView) {
    if (window.Plotly) {
      window.Plotly.purge(state.treemapView);
    }
    state.treemapView = null;
  }
  dom.treemapStage.innerHTML = '';
  dom.treemapStage.appendChild(dom.treemapEmptyState);
}

function showTreemapMessage(message) {
  dom.treemapEmptyState.hidden = false;
  dom.treemapEmptyState.textContent = message;
}

async function ensureRepoFilePaths() {
  if (state.repoFilePaths) {
    return state.repoFilePaths;
  }

  if (state.repoFilePathsPromise) {
    return state.repoFilePathsPromise;
  }

  state.repoFilePathsPromise = (async () => {
    try {
      const trackedPaths = await ensureTrackedRepoFilePaths();
      const fallbackPaths = getCoverageKnownPaths();
      const paths = trackedPaths.length
        ? filterRepoPathsByCoveredEndings(trackedPaths, fallbackPaths)
        : fallbackPaths;
      state.repoFilePaths = paths;
      return state.repoFilePaths;
    } finally {
      state.repoFilePathsPromise = null;
    }
  })();

  return state.repoFilePathsPromise;
}

async function ensureTrackedRepoFilePaths() {
  if (state.repoTrackedPaths) {
    return state.repoTrackedPaths;
  }

  if (state.repoTrackedPathsPromise) {
    return state.repoTrackedPathsPromise;
  }

  state.repoTrackedPathsPromise = (async () => {
    try {
      const gitIndexBuffer = await readFileBytesFromRepo('.git/index');
      state.repoTrackedPaths = parseGitIndexPaths(gitIndexBuffer);
      return state.repoTrackedPaths;
    } catch (_error) {
      state.repoTrackedPaths = [];
      return state.repoTrackedPaths;
    } finally {
      state.repoTrackedPathsPromise = null;
    }
  })();

  return state.repoTrackedPathsPromise;
}

function getCoverageKnownPaths() {
  return [...new Set(
    state.coverageTasks.flatMap((task) => [...task.files.keys()].map((path) => normalizePath(path))),
  )]
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right));
}

function resolveTreemapSelection(nodeId, isFile) {
  if (!nodeId) {
    return null;
  }

  if (isFile) {
    return nodeId;
  }

  const matchingFile = getActiveFileSummaries().find(
    (file) => file.path === nodeId || file.path.startsWith(`${nodeId}/`),
  );
  return matchingFile ? matchingFile.path : null;
}

function filterRepoPathsByCoveredEndings(repoPaths, coveredPaths) {
  const allowedEndings = new Set(coveredPaths.map(getPathEnding).filter((ending) => ending !== null));
  if (!allowedEndings.size) {
    return repoPaths;
  }

  return repoPaths.filter((path) => allowedEndings.has(getPathEnding(path)));
}

function getPathEnding(path) {
  const normalizedPath = normalizePath(path);
  const basename = normalizedPath.split('/').pop() || '';
  const lastDot = basename.lastIndexOf('.');

  if (lastDot <= 0 || lastDot === basename.length - 1) {
    return '(no-extension)';
  }

  return basename.slice(lastDot).toLowerCase();
}

function parseGitIndexPaths(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  const view = new DataView(arrayBuffer);

  if (bytes.length < 12) {
    throw new Error('Git index is too short.');
  }

  const signature = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
  if (signature !== 'DIRC') {
    throw new Error('Git index signature mismatch.');
  }

  const version = view.getUint32(4, false);
  if (version !== 2 && version !== 3) {
    throw new Error(`Unsupported git index version: ${version}`);
  }

  const entryCount = view.getUint32(8, false);
  const trackedPaths = [];
  let offset = 12;

  for (let index = 0; index < entryCount; index += 1) {
    const entryStart = offset;
    if (offset + 62 > bytes.length) {
      throw new Error('Git index entry exceeds file size.');
    }

    offset += 60;
    const flags = view.getUint16(offset, false);
    offset += 2;

    if (version >= 3 && (flags & 0x4000)) {
      if (offset + 2 > bytes.length) {
        throw new Error('Git index extended flags exceed file size.');
      }
      offset += 2;
    }

    let pathEnd = offset;
    while (pathEnd < bytes.length && bytes[pathEnd] !== 0) {
      pathEnd += 1;
    }
    if (pathEnd >= bytes.length) {
      throw new Error('Git index path is not null-terminated.');
    }

    const path = normalizePath(new TextDecoder().decode(bytes.subarray(offset, pathEnd)));
    if (path) {
      trackedPaths.push(path);
    }

    offset = pathEnd + 1;
    while ((offset - entryStart) % 8 !== 0) {
      offset += 1;
    }
  }

  return [...new Set(trackedPaths)].sort((left, right) => left.localeCompare(right));
}

function buildTreemapData(repoFilePaths, activeFileSummaries, fileStatsByPath, currentFile) {
  const summariesByPath = new Map(
    activeFileSummaries.map((file) => [normalizePath(file.path), file]),
  );
  const nodeMap = new Map();
  nodeMap.set('root', {
    id: 'root',
    parent: null,
    name: state.repoSource?.name || 'repo',
    size: 0,
    totalFiles: 0,
    touchedFiles: 0,
    coverageSum: 0,
    coverageRatio: 0,
    coverageLabel: '0%',
    hoverText: '0% average line coverage across files | 0 / 0 files touched',
    isFile: false,
    originalPath: '',
    selected: false,
    depth: 0,
    coveredLineCount: 0,
    totalLines: 0,
  });

  for (const filePath of repoFilePaths) {
    const normalizedPath = normalizePath(filePath);
    const parts = normalizedPath.split('/').filter(Boolean);
    let parentId = 'root';

    for (let index = 0; index < parts.length; index += 1) {
      const isLeaf = index === parts.length - 1;
      const currentPath = parts.slice(0, index + 1).join('/');
      const nodeId = currentPath;

      if (!nodeMap.has(nodeId)) {
        nodeMap.set(nodeId, {
          id: nodeId,
          parent: parentId,
          name: parts[index],
          size: 0,
          totalFiles: 0,
          touchedFiles: 0,
          coverageSum: 0,
          coverageRatio: 0,
          coverageLabel: '0%',
          hoverText: '',
          isFile: isLeaf,
          originalPath: isLeaf ? normalizedPath : '',
          selected: normalizedPath === currentFile,
          depth: index + 1,
          coveredLineCount: 0,
          totalLines: 0,
        });
      }

      parentId = nodeId;
    }

    const leaf = nodeMap.get(normalizedPath);
    const summary = summariesByPath.get(normalizedPath) || null;
    const coveredLineCount = summary?.coveredLineCount || 0;
    const totalLines = fileStatsByPath.get(normalizedPath)?.totalLines || 0;

    leaf.size = 1;
    leaf.totalFiles = 1;
    leaf.touchedFiles = coveredLineCount > 0 ? 1 : 0;
    leaf.coverageSum = calculateCoverageRatio(coveredLineCount, totalLines);
    leaf.coverageRatio = leaf.coverageSum;
    leaf.coverageLabel = formatTreemapFileCoverageLabel(coveredLineCount, totalLines);
    leaf.hoverText = buildTreemapFileHoverText(coveredLineCount, totalLines);
    leaf.selected = normalizedPath === currentFile;
    leaf.coveredLineCount = coveredLineCount;
    leaf.totalLines = totalLines;
  }

  const nodesByDepth = [...nodeMap.values()].sort((left, right) => right.depth - left.depth);
  for (const node of nodesByDepth) {
    if (node.id === 'root' || node.isFile) {
      continue;
    }

    const children = [...nodeMap.values()].filter((candidate) => candidate.parent === node.id);
    node.size = children.reduce((sum, child) => sum + child.size, 0);
    node.totalFiles = children.reduce((sum, child) => sum + child.totalFiles, 0);
    node.touchedFiles = children.reduce((sum, child) => sum + child.touchedFiles, 0);
    node.coverageSum = children.reduce((sum, child) => sum + child.coverageSum, 0);
    node.coverageRatio = node.totalFiles ? node.coverageSum / node.totalFiles : 0;
    node.coverageLabel = formatRatioPercent(node.coverageRatio);
    node.hoverText = buildTreemapDirectoryHoverText(node.coverageRatio, node.touchedFiles, node.totalFiles);
    node.selected = children.some((child) => child.selected);
  }

  const root = nodeMap.get('root');
  const rootChildren = [...nodeMap.values()].filter((candidate) => candidate.parent === 'root');
  root.size = rootChildren.reduce((sum, child) => sum + child.size, 0);
  root.totalFiles = rootChildren.reduce((sum, child) => sum + child.totalFiles, 0);
  root.touchedFiles = rootChildren.reduce((sum, child) => sum + child.touchedFiles, 0);
  root.coverageSum = rootChildren.reduce((sum, child) => sum + child.coverageSum, 0);
  root.coverageRatio = root.totalFiles ? root.coverageSum / root.totalFiles : 0;
  root.coverageLabel = formatRatioPercent(root.coverageRatio);
  root.hoverText = buildTreemapDirectoryHoverText(root.coverageRatio, root.touchedFiles, root.totalFiles);
  root.selected = rootChildren.some((child) => child.selected);

  return [...nodeMap.values()];
}

function buildTreemapTrace(treeRows) {
  const nodes = treeRows
    .filter((node) => node.id === 'root' || node.totalFiles > 0)
    .sort((left, right) => left.depth - right.depth || left.id.localeCompare(right.id));

  return {
    type: 'treemap',
    ids: nodes.map((node) => node.id),
    labels: nodes.map((node) => node.name),
    parents: nodes.map((node) => node.parent || ''),
    values: nodes.map((node) => node.size || 1),
    branchvalues: 'total',
    texttemplate: '%{label}<br>%{customdata[0]}',
    textfont: {
      family: 'Avenir Next, Helvetica Neue, sans-serif',
      color: nodes.map((node) => (node.coverageRatio != null && node.coverageRatio > 0.8 ? '#ffffff' : '#181713')),
      size: 15,
    },
    customdata: nodes.map((node) => [
      node.coverageLabel,
      node.originalPath || node.id,
      node.hoverText,
      node.isFile ? 'file' : 'directory',
    ]),
    marker: {
      colors: nodes.map((node) => node.coverageRatio),
      colorscale: [
        [0, '#e9e1d3'],
        [0.38, '#d7e2d8'],
        [0.72, '#87ae93'],
        [1, '#1f5a46'],
      ],
      cmin: 0,
      cmax: 1,
      line: {
        width: nodes.map((node) => (node.selected ? 3 : 1)),
        color: nodes.map((node) => (node.selected ? '#9a5d28' : 'rgba(255, 255, 255, 0.34)')),
      },
    },
    hovertemplate: '%{customdata[1]}<br>%{customdata[2]}<extra></extra>',
    tiling: { pad: 3 },
    pathbar: {
      textfont: {
        family: 'Avenir Next, Helvetica Neue, sans-serif',
        size: 13,
        color: '#181713',
      },
    },
    root: {
      color: 'rgba(0,0,0,0)',
    },
  };
}

function buildTreemapLayout() {
  return {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    margin: { t: 12, r: 0, b: 0, l: 0 },
    font: {
      family: 'Avenir Next, Helvetica Neue, sans-serif',
      color: '#181713',
    },
  };
}

function formatRatioPercent(ratio) {
  if (!Number.isFinite(ratio) || ratio <= 0) {
    return '0%';
  }

  const percentage = Math.min(1, ratio) * 100;
  if (percentage >= 99.5) {
    return '100%';
  }
  if (percentage < 1) {
    return '<1%';
  }
  return `${Math.round(percentage)}%`;
}

function buildTree(files) {
  const root = {
    name: '',
    path: '',
    directories: new Map(),
    files: [],
  };

  for (const file of files) {
    const parts = file.path.split('/');
    let node = root;
    let currentPath = '';

    for (const part of parts.slice(0, -1)) {
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      if (!node.directories.has(part)) {
        node.directories.set(part, {
          name: part,
          path: currentPath,
          directories: new Map(),
          files: [],
        });
      }
      node = node.directories.get(part);
    }

    node.files.push(file);
  }

  return root;
}

function renderTreeDirectory(node, isRoot = false) {
  const directories = Array.from(node.directories.values()).sort((left, right) =>
    left.name.localeCompare(right.name),
  );
  const files = [...node.files].sort((left, right) => left.path.localeCompare(right.path));

  const childrenMarkup = directories
    .map((directory) => renderTreeDirectory(directory))
    .concat(files.map((file) => renderFileButton(file)))
    .join('');

  if (isRoot) {
    return childrenMarkup;
  }

  return `
    <details class="tree-directory" open>
      <summary>${escapeHtml(node.name)}</summary>
      ${childrenMarkup}
    </details>
  `;
}

function renderFileButton(file) {
  const isActive = file.path === state.currentFile;
  const classes = isActive ? 'file-button active' : 'file-button';
  const leafName = file.path.split('/').pop();
  const fileStats = state.fileStatsCache.get(file.path);
  const coverageBadge = fileStats
    ? formatCoveragePercent(file.coveredLineCount, fileStats.totalLines)
    : '--';

  return `
    <button
      type="button"
      class="${classes}"
      data-file-path="${escapeAttribute(file.path)}"
      title="${escapeAttribute(file.path)}"
    >
      <div class="file-button-title">
        <span class="file-button-path">${escapeHtml(leafName)}</span>
        <span class="count-badge">${escapeHtml(coverageBadge)}</span>
      </div>
      <div class="file-button-meta">
        <span>${numberFormatter.format(file.taskCount)} items</span>
        <span>${numberFormatter.format(file.commandCount)} cmds</span>
      </div>
    </button>
  `;
}

function renderCodeView() {
  const currentFileSummary = getCurrentFileSummary();
  const previousPath = dom.codeView.dataset.path || '';
  const shouldPreserveScroll = previousPath === state.currentFile;
  const previousScrollTop = shouldPreserveScroll ? dom.codeView.scrollTop : 0;
  const previousScrollLeft = shouldPreserveScroll ? dom.codeView.scrollLeft : 0;
  dom.currentFilePath.textContent = state.currentFile || '-';

  if (!state.currentFile) {
    dom.currentFileMeta.textContent = 'Select a file';
    dom.codeView.className = 'code-view empty-state';
    dom.codeView.textContent = 'No file';
    dom.codeView.dataset.path = '';
    return;
  }

  if (!state.repoSource) {
    dom.currentFileMeta.textContent = 'No repo';
    dom.codeView.className = 'code-view empty-state';
    dom.codeView.textContent = 'Load repo';
    dom.codeView.dataset.path = '';
    return;
  }

  if (!state.currentFileView) {
    dom.currentFileMeta.textContent = currentFileSummary
      ? `${numberFormatter.format(currentFileSummary.coveredLineCount)} covered • ${numberFormatter.format(currentFileSummary.taskCount)} items`
      : 'Loading';
    dom.codeView.className = 'code-view empty-state';
    dom.codeView.textContent = 'Loading';
    dom.codeView.dataset.path = state.currentFile;
    return;
  }

  if (state.currentFileView.missing) {
    dom.currentFileMeta.textContent = 'Unavailable';
    dom.codeView.className = 'code-view';
    dom.codeView.innerHTML = `<div class="warning-box">${escapeHtml(state.currentFileView.error)}</div>`;
    dom.codeView.dataset.path = state.currentFile;
    return;
  }

  const { lines, lineMeta } = state.currentFileView;
  dom.currentFileMeta.textContent = [
    `${numberFormatter.format(lines.length)} lines`,
    currentFileSummary
      ? `${numberFormatter.format(currentFileSummary.coveredLineCount)} covered`
      : null,
    state.currentLine ? `line ${numberFormatter.format(state.currentLine)}` : null,
  ]
    .filter(Boolean)
    .join(' • ');

  const lineMarkup = lines
    .map((line, index) => {
      const lineNumber = index + 1;
      const meta = lineMeta[lineNumber];
      const selectedCount = meta ? meta.selectedTaskIds.size : 0;
      const depthClass = selectedCount ? `covered-depth-${Math.min(selectedCount, 4)}` : '';
      const selectedClass = state.currentLine === lineNumber ? 'selected' : '';
      const lineClasses = ['code-line', depthClass, selectedClass].filter(Boolean).join(' ');
      const hitContent = selectedCount
        ? `<span>${numberFormatter.format(selectedCount)}</span>`
        : '';

      return `
        <div class="${lineClasses}" data-line-number="${lineNumber}">
          <div class="line-number">${lineNumber}</div>
          <div class="line-hit-count">${hitContent}</div>
          <pre class="line-code">${escapeHtml(line || ' ')}</pre>
        </div>
      `;
    })
    .join('');

  dom.codeView.className = 'code-view';
  dom.codeView.innerHTML = `<div class="code-lines">${lineMarkup}</div>`;
  dom.codeView.dataset.path = state.currentFile;
  if (shouldPreserveScroll) {
    dom.codeView.scrollTop = previousScrollTop;
    dom.codeView.scrollLeft = previousScrollLeft;
  } else {
    dom.codeView.scrollTop = 0;
    dom.codeView.scrollLeft = 0;
  }
}

function renderInspector() {
  if (!state.currentFile) {
    dom.inspectorContent.className = 'inspector-content empty-state';
    dom.inspectorContent.textContent = 'Select a file or line';
    return;
  }

  if (state.currentLine && state.currentFileView && !state.currentFileView.missing) {
    renderLineInspector();
    return;
  }

  renderFileInspector();
}

function renderFileInspector() {
  const details = collectFileTaskDetails(state.currentFile);
  const showSections = details.hidden.length > 0;
  const markup = [
    `<div class="inspector-note">`,
    `<p><strong>${escapeHtml(state.currentFile)}</strong></p>`,
    `<p class="status-text">${numberFormatter.format(details.selected.length)} visible${details.hidden.length ? ` • ${numberFormatter.format(details.hidden.length)} hidden` : ''}</p>`,
    `</div>`,
    renderDetailSection('Visible', details.selected, 'file', showSections),
    details.hidden.length
      ? renderDetailSection('Hidden', details.hidden, 'file', true)
      : '',
  ]
    .filter(Boolean)
    .join('');

  dom.inspectorContent.className = 'inspector-content';
  dom.inspectorContent.innerHTML = markup;
}

function renderLineInspector() {
  const details = collectLineTaskDetails(state.currentFile, state.currentLine);
  const selectedCount = details.selected.length;
  const hiddenCount = details.hidden.length;
  const showSections = hiddenCount > 0;

  const markup = [
    `<div class="inspector-note">`,
    `<p><strong>${escapeHtml(state.currentFile)}:${numberFormatter.format(state.currentLine)}</strong></p>`,
    `<p class="status-text">${selectedCount ? `${numberFormatter.format(selectedCount)} visible` : 'No visible items'}</p>`,
    hiddenCount
      ? `<p class="status-text">${numberFormatter.format(hiddenCount)} hidden</p>`
      : '',
    `</div>`,
    renderDetailSection('Visible', details.selected, 'line', showSections),
    hiddenCount ? renderDetailSection('Hidden', details.hidden, 'line', true) : '',
  ]
    .filter(Boolean)
    .join('');

  dom.inspectorContent.className = 'inspector-content';
  dom.inspectorContent.innerHTML = markup;
}

function renderDetailSection(title, detailItems, mode, showTitle = true) {
  if (!detailItems.length) {
    return `
      <div class="section-stack">
        ${showTitle ? `<p class="section-label">${escapeHtml(title)}</p>` : ''}
        <div class="empty-state">Nothing here</div>
      </div>
    `;
  }

  const cards = detailItems
    .map((detail) => {
      const subtitle = mode === 'line'
        ? `${numberFormatter.format(detail.commandGroups.length)} cmds`
        : `${numberFormatter.format(detail.coveredLineCount)} lines`;
      const contextLabel = detail.task.fullPath || detail.task.ownerPath || detail.task.prompt;

      return `
        <article class="detail-card">
          <div class="detail-card-header">
            <span class="detail-kind">${escapeHtml(primaryOwnerKind(detail.task))}</span>
            <span class="count-badge">${escapeHtml(subtitle)}</span>
          </div>
          ${renderDetailPath(detail.task, contextLabel)}
          <div class="command-list">
            ${detail.commandGroups.map(renderCommandCard).join('')}
          </div>
        </article>
      `;
    })
    .join('');

  return `
    <div class="section-stack">
      ${showTitle ? `<p class="section-label">${escapeHtml(title)}</p>` : ''}
      ${cards}
    </div>
  `;
}

function renderDetailPath(task, fallbackLabel) {
  const labels = Array.isArray(task?.fullLineageLabels) && task.fullLineageLabels.length
    ? task.fullLineageLabels
    : [fallbackLabel];

  const rows = labels
    .map((label, index) => {
      const separator = index ? '/' : '';
      return `
        <div class="detail-path-row">
          <span class="detail-path-separator" aria-hidden="true">${separator}</span>
          <span class="detail-path-step">${escapeHtml(label)}</span>
        </div>
      `;
    })
    .join('');

  return `
    <div class="detail-path" title="${escapeAttribute(fallbackLabel)}">
      <div class="detail-path-label">Path</div>
      <div class="detail-path-steps">${rows}</div>
    </div>
  `;
}

function renderCommandCard(commandGroup) {
  const rangeMarkup = commandGroup.ranges
    .map((range) => {
      const label = range.start === range.end
        ? `${range.path}:${range.start}`
        : `${range.path}:${range.start}-${range.end}`;
      return `<span class="range-pill">${escapeHtml(label)}</span>`;
    })
    .join('');

  return `
    <div class="command-card">
      <code>${escapeHtml(commandGroup.cmd || '(empty command)')}</code>
      <div class="range-list">${rangeMarkup}</div>
    </div>
  `;
}

function updateRepoStatus(message, isWarning = false) {
  dom.repoStatus.textContent = message;
  dom.repoStatus.className = isWarning ? 'status-text warning-text' : 'status-text';
}

function updateCoverageStatus(message, isWarning = false) {
  dom.coverageStatus.textContent = message;
  dom.coverageStatus.className = isWarning ? 'status-text warning-text' : 'status-text';
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll('`', '&#96;');
}

function formatCoveragePercent(coveredLineCount, totalLines) {
  if (!totalLines) {
    return '--';
  }

  const percentage = calculateCoverageRatio(coveredLineCount, totalLines) * 100;
  if (percentage >= 99.5) {
    return '100%';
  }
  if (percentage > 0 && percentage < 1) {
    return '<1%';
  }
  return `${Math.round(percentage)}%`;
}

function calculateCoverageRatio(coveredLineCount, totalLines) {
  if (!Number.isFinite(totalLines) || totalLines <= 0) {
    return 0;
  }

  if (!Number.isFinite(coveredLineCount) || coveredLineCount <= 0) {
    return 0;
  }

  return Math.min(1, coveredLineCount / totalLines);
}

function formatTreemapFileCoverageLabel(coveredLineCount, totalLines) {
  if (coveredLineCount <= 0) {
    return '0%';
  }

  if (!totalLines) {
    return '--';
  }

  return formatCoveragePercent(coveredLineCount, totalLines);
}

function buildTreemapFileHoverText(coveredLineCount, totalLines) {
  if (!coveredLineCount) {
    return '0% of lines covered';
  }

  if (!totalLines) {
    return `${numberFormatter.format(coveredLineCount)} covered lines | total line count unavailable`;
  }

  return [
    `${formatCoveragePercent(coveredLineCount, totalLines)} of lines covered`,
    `(${numberFormatter.format(coveredLineCount)} / ${numberFormatter.format(totalLines)} lines)`,
  ].join(' ');
}

function buildTreemapDirectoryHoverText(coverageRatio, touchedFiles, totalFiles) {
  return [
    `${formatRatioPercent(coverageRatio)} average line coverage across files`,
    `${numberFormatter.format(touchedFiles)} / ${numberFormatter.format(totalFiles)} files touched`,
  ].join(' | ');
}
