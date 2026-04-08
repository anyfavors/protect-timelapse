/* Protect Timelapse — Alpine.js SPA
 * Single global component: timelapseApp()
 * Script must be loaded with <script defer> BEFORE the Alpine CDN tag.
 */

document.addEventListener('alpine:init', () => {
  Alpine.data('timelapseApp', () => ({
    // ── State ─────────────────────────────────────────────────────────────
    darkMode: localStorage.getItem('darkMode') !== 'false',

    view: 'dashboard',        // dashboard | project_detail | create_project | settings | cameras | renders_queue
    allRenders: [],
    scrubberHoverFrame: null,
    scrubberHoverX: 0,
    _scrubberHoverTimer: null,
    projects: [],
    cameras: [],
    activeProject: null,
    activeProjectFrames: [],
    activeProjectRenders: [],
    activeProjectBookmarks: [],
    activeProjectDarkFrames: [],
    activeProjectBlurryFrames: [],
    activeProjectDailyStats: [],
    activeProjectTimeline: [],
    templates: [],
    notifications: [],
    unreadCount: 0,
    ws: null,
    wsRetries: 0,
    wsMaxRetries: 3,
    diskSpace: { free_gb: null, total_gb: null },
    systemStatus: null,
    _systemStatusTimer: null,
    diskThreshold: 5,
    toasts: [],
    showNotifDropdown: false,
    showShortcutHelp: false,
    showDiskModal: false,
    diskBreakdown: {},
    selectedProjectIds: [],
    selectMode: false,
    historicalPresetLabel: '',
    historicalPresets: [
      { label: 'Last 24 h',   hours: 24 },
      { label: 'Last 7 days', hours: 24 * 7 },
      { label: 'Last 30 days', hours: 24 * 30 },
      { label: 'This week',   week: 'current' },
      { label: 'Last week',   week: 'prev' },
      { label: 'This month',  month: 'current' },
      { label: 'Last month',  month: 'prev' },
      { label: 'Custom',      custom: true },
    ],

    // Detail view tabs
    detailTab: 'overview',    // overview | renders | bookmarks | dark_frames
    scrubberIndex: 0,
    scrubberFrameUrl: null,
    scrubberTimestamp: null,
    rangeStart: null,
    rangeEnd: null,
    shiftDragging: false,

    // Create/Edit form
    form: {},
    formMode: 'create',       // create | edit
    previewUrl: null,
    previewTimer: null,
    selectedTemplate: null,

    // GIF export
    gifJobStatus: null,   // null | 'pending' | 'rendering' | 'done' | 'error'
    gifPollTimer: null,

    // Render settings
    renderFramerate: 30,
    renderResolution: '1920x1080',
    renderQuality: 'standard',
    renderFlicker: 'standard',
    renderFrameBlend: false,
    renderStabilize: false,
    renderColorGrade: 'none',
    renderPresets: [],

    // Scrubber quality filter
    scrubberQualityFilter: 'all',  // all | dark | blurry | bookmarked

    // Interval analyzer
    intervalAnalyzer: { targetMinutes: 2, targetFps: 30, result: null },

    // Pinned / muted state
    mutedProjectIds: [],

    // Drag-to-reorder state for render queue
    dragRenderFrom: null,

    // Render comparison & video player
    compareRenders: [],
    videoPlaybackRate: 1,
    comparisonSyncing: false,

    // Hourly timeline chart
    timelineMaxCount: 1,

    // ── Lifecycle ─────────────────────────────────────────────────────────
    async init() {
      // Load settings first so theme is applied before anything renders
      const s = await this.api('/api/settings');
      if (s) {
        this.loadTheme(s.dark_mode);
        if (s.muted_project_ids) {
          try { this.mutedProjectIds = JSON.parse(s.muted_project_ids); } catch(e) { this.mutedProjectIds = []; }
        }
      } else this.loadTheme(true);
      this.initKeyboardShortcuts();
      await this.loadAll();
      this.connectWebSocket();
      // Real-time render estimate: update whenever framerate or resolution changes (UX5)
      this.$watch('renderFramerate', () => this.updateRenderEstimate(this.renderFramerate));
      this.$watch('renderResolution', () => this.updateRenderEstimate(this.renderFramerate));
    },

    loadTheme(darkMode) {
      this.darkMode = darkMode !== false && darkMode !== 0;
      document.documentElement.classList.toggle('dark', this.darkMode);
    },

    async toggleDark() {
      this.darkMode = !this.darkMode;
      document.documentElement.classList.toggle('dark', this.darkMode);
      localStorage.setItem('darkMode', String(this.darkMode));
      // Keep settingsData in sync so saveSettings() doesn't overwrite the toggle
      if (this.settingsData) this.settingsData.dark_mode = this.darkMode ? 1 : 0;
      // Await save — revert on failure so UI stays consistent (#29)
      const result = await this.api('/api/settings', 'PUT', { dark_mode: this.darkMode });
      if (!result) {
        this.darkMode = !this.darkMode;
        document.documentElement.classList.toggle('dark', this.darkMode);
        localStorage.setItem('darkMode', String(this.darkMode));
        if (this.settingsData) this.settingsData.dark_mode = this.darkMode ? 1 : 0;
      }
    },

    async loadAll() {
      await Promise.all([
        this.loadProjects(),
        this.loadCameras(),
        this.loadTemplates(),
        this.loadNotifications(),
        this.loadHealth(),
        this.loadSystemStatus(),
        this.loadPresets(),
      ]);
      // Poll system status every 30s
      if (this._systemStatusTimer) clearInterval(this._systemStatusTimer);
      this._systemStatusTimer = setInterval(() => this.loadSystemStatus(), 30000);
    },

    // ── Data loaders ──────────────────────────────────────────────────────
    async loadProjects() {
      const data = await this.api('/api/projects');
      if (data) this.projects = data;
    },

    async loadCameras() {
      const data = await this.api('/api/cameras');
      if (data) this.cameras = data;
    },

    async openCameraGrid() {
      this.view = 'cameras';
      await this.loadCameras();
      this._startCameraGridAutoRefresh();
    },

    _cameraGridTimer: null,

    _startCameraGridAutoRefresh() {
      this._stopCameraGridAutoRefresh();
      this._cameraGridTimer = setInterval(() => {
        if (this.view !== 'cameras') { this._stopCameraGridAutoRefresh(); return; }
        // bump preview image timestamps by forcing reactive update
        this._cameraPreviewTs = Date.now();
      }, 5000);
    },

    _stopCameraGridAutoRefresh() {
      if (this._cameraGridTimer) { clearInterval(this._cameraGridTimer); this._cameraGridTimer = null; }
    },

    _cameraPreviewTs: Date.now(),

    cameraPreviewUrl(cameraId) {
      return `/api/cameras/${cameraId}/preview?t=${this._cameraPreviewTs}`;
    },

    async refreshCameraGrid() {
      this._cameraPreviewTs = Date.now();
      await this.loadCameras();
    },

    createProjectFromCamera(cameraId) {
      this.openCreateForm();
      this.$nextTick(() => { this.form.camera_id = cameraId; });
    },

    async loadTemplates() {
      const data = await this.api('/api/templates');
      if (data) this.templates = data;
    },

    async loadNotifications() {
      const data = await this.api('/api/notifications?limit=50');
      if (data) {
        this.notifications = data;
        this.unreadCount = data.filter(n => !n.is_read).length;
      }
    },

    async loadHealth() {
      const data = await this.api('/api/health');
      if (data) {
        this.diskSpace = { free_gb: data.disk_free_gb, total_gb: data.disk_total_gb };
      }
    },

    async loadSystemStatus() {
      const data = await this.api('/api/system/status');
      if (data) {
        this.systemStatus = data;
        // Keep disk space in sync
        if (data.disk) {
          this.diskSpace = { free_gb: data.disk.free_gb, total_gb: data.disk.total_gb };
        }
      }
    },

    async retryExtraction(projectId) {
      const r = await this.api(`/api/projects/${projectId}/retry-extraction`, 'POST');
      if (r) {
        this.toast('Extraction restarted (will resume from last checkpoint)', 'success', projectId);
        const p = this.projects.find(p => p.id === projectId);
        if (p) p.status = 'extracting';
      }
    },

    async openDiskModal() {
      this.showDiskModal = true;
      const data = await this.api('/api/disk');
      if (data) this.diskBreakdown = data;
    },

    // ── Camera grouping ───────────────────────────────────────────────────
    get cameraGroups() {
      const map = {};
      for (const cam of this.cameras) {
        map[cam.id] = { ...cam, projects: [] };
      }
      for (const proj of this.projects) {
        if (map[proj.camera_id]) {
          map[proj.camera_id].projects.push(proj);
        } else {
          // Camera no longer on NVR but project references it
          map[proj.camera_id] = {
            id: proj.camera_id, name: proj.camera_id, is_online: false, is_connected: false, projects: [proj]
          };
        }
      }
      return Object.values(map);
    },

    cameraHasError(cam) {
      // Use is_online (API field name) consistently — is_connected was wrong (#13)
      return cam.projects.some(p => p.status === 'paused_error' || p.status === 'error')
        || !cam.is_online;
    },

    // ── Disk gauge ────────────────────────────────────────────────────────
    get diskPercent() {
      if (!this.diskSpace.total_gb || this.diskSpace.total_gb <= 0) return 0;
      return Math.round((1 - this.diskSpace.free_gb / this.diskSpace.total_gb) * 100);
    },

    get diskCritical() {
      return this.diskSpace.free_gb !== null && this.diskSpace.free_gb < this.diskThreshold;
    },

    // ── Project actions ───────────────────────────────────────────────────
    async pauseProject(id) {
      await this.api(`/api/projects/${id}`, 'PUT', { status: 'paused' });
      await this.loadProjects();
    },

    async resumeProject(id) {
      await this.api(`/api/projects/${id}`, 'PUT', { status: 'active' });
      await this.loadProjects();
    },

    async deleteProject(id) {
      if (!await this.confirm('Delete this project and all its frames?', 'Delete Project')) return;
      await this.api(`/api/projects/${id}`, 'DELETE');
      await this.loadProjects();
      if (this.activeProject?.id === id) this.view = 'dashboard';
      this.toast('Project deleted');
    },

    async cloneProject(id) {
      const data = await this.api(`/api/projects/${id}/clone`, 'POST');
      if (data) {
        await this.loadProjects();
        this.toast(`Project cloned as "${data.name}"`);
      }
    },

    // ── Bulk project actions ──────────────────────────────────────────────
    toggleSelectMode() {
      this.selectMode = !this.selectMode;
      if (!this.selectMode) this.selectedProjectIds = [];
    },

    toggleSelectProject(id) {
      if (this.selectedProjectIds.includes(id)) {
        this.selectedProjectIds = this.selectedProjectIds.filter(x => x !== id);
      } else {
        this.selectedProjectIds.push(id);
      }
    },

    async bulkPause() {
      const ids = [...this.selectedProjectIds];
      await this._runBatch(ids, 'Pausing', id => this.api(`/api/projects/${id}`, 'PATCH', { status: 'paused' }));
      await this.loadProjects();
      this.toast(`Paused ${ids.length} project(s)`);
      this.selectedProjectIds = [];
    },

    async bulkResume() {
      const ids = [...this.selectedProjectIds];
      await this._runBatch(ids, 'Resuming', id => this.api(`/api/projects/${id}`, 'PATCH', { status: 'active' }));
      await this.loadProjects();
      this.toast(`Resumed ${ids.length} project(s)`);
      this.selectedProjectIds = [];
    },

    // ── Select all (UX3) ─────────────────────────────────────────────────
    get allSelected() {
      return this.projects.length > 0 && this.projects.every(p => this.selectedProjectIds.includes(p.id));
    },

    toggleSelectAll() {
      if (this.allSelected) {
        this.selectedProjectIds = [];
      } else {
        this.selectedProjectIds = this.projects.map(p => p.id);
      }
    },

    async bulkDelete() {
      if (!await this.confirm(`Delete ${this.selectedProjectIds.length} project(s)?`, 'Delete Projects')) return;
      const ids = [...this.selectedProjectIds];
      await this._runBatch(ids, 'Deleting', id => this.api(`/api/projects/${id}`, 'DELETE'));
      await this.loadProjects();
      this.toast(`Deleted ${ids.length} project(s)`);
      this.selectedProjectIds = [];
    },

    // ── Project detail ────────────────────────────────────────────────────
    async openProject(project) {
      // Clear any pending undo toasts from previous view before navigating
      this.toasts = this.toasts.filter(t => !t.undoId);
      this.activeProject = project;
      this.detailTab = 'overview';
      this.view = 'project_detail';
      this.scrubberIndex = 0;
      this.scrubberQualityFilter = 'all';
      this.framePage = 0;  // reset pagination (FE3)
      this.rangeStart = null;
      this.rangeEnd = null;
      this.gifJobStatus = null;
      if (this.gifPollTimer) { clearInterval(this.gifPollTimer); this.gifPollTimer = null; }
      await this.loadProjectDetail();
    },

    async loadProjectDetail() {
      if (!this.activeProject) return;
      const id = this.activeProject.id;
      // Use allSettled so one failing request doesn't abort the rest (FE2)
      const [framesR, rendersR, dailyR, timelineR] = await Promise.allSettled([
        this.api(`/api/projects/${id}/frames?fields=id,captured_at&limit=500`),
        this.api(`/api/projects/${id}/renders`),
        this.api(`/api/projects/${id}/stats/daily`),
        this.api(`/api/projects/${id}/stats/timeline`),
      ]);
      const frames = framesR.status === 'fulfilled' ? framesR.value : null;
      const renders = rendersR.status === 'fulfilled' ? rendersR.value : null;
      const daily = dailyR.status === 'fulfilled' ? dailyR.value : null;
      const timeline = timelineR.status === 'fulfilled' ? timelineR.value : null;
      if (frames) this.activeProjectFrames = frames;
      if (renders) this.activeProjectRenders = renders;
      if (daily) this.activeProjectDailyStats = daily;
      if (timeline) {
        this.activeProjectTimeline = timeline;
        this.timelineMaxCount = Math.max(1, ...timeline.map(r => r.captured));
      }
      this.updateScrubberFrame();
    },

    async loadDetailTab(tab) {
      this.detailTab = tab;
      const id = this.activeProject?.id;
      if (!id) return;
      if (tab === 'bookmarks') {
        const data = await this.api(`/api/projects/${id}/frames/bookmarks`);
        if (data) this.activeProjectBookmarks = data;
      } else if (tab === 'quality') {
        const [dark, blurry] = await Promise.all([
          this.api(`/api/projects/${id}/frames/dark`),
          this.api(`/api/projects/${id}/frames/blurry`),
        ]);
        if (dark) this.activeProjectDarkFrames = dark;
        if (blurry) this.activeProjectBlurryFrames = blurry;
      } else if (tab === 'renders') {
        const data = await this.api(`/api/projects/${id}/renders`);
        if (data) this.activeProjectRenders = data;
      }
    },

    // ── Scrubber ──────────────────────────────────────────────────────────
    updateScrubberFrame() {
      const frames = this.activeProjectFrames;
      if (!frames.length) { this.scrubberFrameUrl = null; return; }
      const idx = Math.min(this.scrubberIndex, frames.length - 1);
      const frame = frames[idx];
      this.scrubberFrameUrl = `/api/projects/${this.activeProject.id}/frames/${frame.id}/thumbnail`;
      this.scrubberTimestamp = frame.captured_at;
    },

    onScrubberInput(e) {
      this.scrubberIndex = parseInt(e.target.value);
      this.updateScrubberFrame();
    },

    onScrubberHover(e) {
      if (!this.activeProjectFrames.length) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const idx = Math.round(ratio * (this.activeProjectFrames.length - 1));
      this.scrubberHoverFrame = this.activeProjectFrames[idx] || null;
      this.scrubberHoverX = e.clientX - rect.left;
    },

    onScrubberShiftClick(e) {
      if (!e.shiftKey) return;
      const idx = parseInt(e.target.value);
      const frame = this.activeProjectFrames[idx];
      if (!frame) return;
      if (!this.rangeStart) {
        this.rangeStart = frame.captured_at;
      } else {
        this.rangeEnd = frame.captured_at;
      }
    },

    async deleteFrame(frameId) {
      if (!await this.confirm('Delete this frame permanently?', 'Delete Frame')) return;
      const id = this.activeProject.id;
      await this.api(`/api/projects/${id}/frames/${frameId}`, 'DELETE');
      this.activeProjectFrames = this.activeProjectFrames.filter(f => f.id !== frameId);
      this.activeProjectBookmarks = this.activeProjectBookmarks.filter(f => f.id !== frameId);
      this.activeProjectDarkFrames = this.activeProjectDarkFrames.filter(f => f.id !== frameId);
      this.activeProjectBlurryFrames = this.activeProjectBlurryFrames.filter(f => f.id !== frameId);
      if (this.activeProject) this.activeProject.frame_count = Math.max(0, (this.activeProject.frame_count || 1) - 1);
      if (this.scrubberIndex >= this.activeProjectFrames.length) {
        this.scrubberIndex = Math.max(0, this.activeProjectFrames.length - 1);
      }
      this.updateScrubberFrame();
      this.toast('Frame deleted');
    },

    async bookmarkCurrentFrame(note) {
      const frame = this.activeProjectFrames[this.scrubberIndex];
      if (!frame) return;
      await this.api(
        `/api/projects/${this.activeProject.id}/frames/${frame.id}/bookmark`,
        'PUT', { note }
      );
      this.toast('Frame bookmarked');
    },

    // ── Frame split-view comparison ───────────────────────────────────────
    compareFrameIndices: [],

    markCompareFrame() {
      const idx = this.scrubberIndex;
      const pos = this.compareFrameIndices.indexOf(idx);
      if (pos >= 0) {
        this.compareFrameIndices.splice(pos, 1);
        this.toast('Frame deselected for comparison');
      } else if (this.compareFrameIndices.length < 2) {
        this.compareFrameIndices.push(idx);
        if (this.compareFrameIndices.length === 2) this.toast('Two frames selected — opening comparison');
        else this.toast('Frame marked — scrub to another frame and press C again');
      } else {
        // Replace oldest selection
        this.compareFrameIndices = [this.compareFrameIndices[1], idx];
        this.toast('Comparison frame updated');
      }
      if (this.compareFrameIndices.length === 2) this.openFrameComparison();
    },

    openFrameComparison() {
      const pid = this.activeProject.id;
      const frames = this.activeProjectFrames;
      const [idxA, idxB] = this.compareFrameIndices;
      const frameA = frames[idxA], frameB = frames[idxB];
      if (!frameA || !frameB) return;

      const urlA = `/api/projects/${pid}/frames/${frameA.id}/full`;
      const urlB = `/api/projects/${pid}/frames/${frameB.id}/full`;
      const tsA = this.formatDate(frameA.captured_at);
      const tsB = this.formatDate(frameB.captured_at);

      const overlay = document.createElement('div');
      overlay.id = 'frame-compare-overlay';
      overlay.className = 'fixed inset-0 z-50 flex flex-col bg-black/95 p-4';
      overlay.innerHTML = `
        <div class="flex items-center justify-between mb-3">
          <span class="text-sm text-slate-300">Frame Comparison — drag divider to compare</span>
          <button id="fcomp-close-btn"
                  class="text-slate-400 hover:text-white text-sm bg-slate-800 px-4 py-2 rounded-lg">Close [Esc]</button>
        </div>
        <div id="fcomp-container" class="relative flex-1 overflow-hidden rounded-xl bg-black select-none">
          <!-- Right frame (full width, behind clip) -->
          <img id="fcomp-b" src="${urlB}" alt=""
               class="absolute inset-0 w-full h-full object-contain" draggable="false" />
          <!-- Left frame (clipped by divider position) -->
          <div id="fcomp-clip" class="absolute inset-0 overflow-hidden" style="width:50%">
            <img id="fcomp-a" src="${urlA}" alt=""
                 class="absolute inset-0 w-full h-full object-contain" style="width:200%;max-width:none" draggable="false" />
          </div>
          <!-- Divider line -->
          <div id="fcomp-divider"
               class="absolute top-0 bottom-0 w-0.5 bg-white/80 cursor-col-resize z-10 flex items-center justify-center"
               style="left:50%">
            <div class="w-6 h-6 rounded-full bg-white/90 flex items-center justify-center shadow-lg text-slate-800 text-xs font-bold">⇔</div>
          </div>
          <!-- Timestamps -->
          <div class="absolute bottom-3 left-3 text-xs text-white bg-black/60 px-2 py-1 rounded">${tsA}</div>
          <div class="absolute bottom-3 right-3 text-xs text-white bg-black/60 px-2 py-1 rounded">${tsB}</div>
        </div>`;

      document.body.appendChild(overlay);

      // Drag logic
      const container = document.getElementById('fcomp-container');
      const divider   = document.getElementById('fcomp-divider');
      const clip      = document.getElementById('fcomp-clip');
      const imgA      = document.getElementById('fcomp-a');

      const setPos = pct => {
        pct = Math.max(5, Math.min(95, pct));
        divider.style.left = pct + '%';
        clip.style.width   = pct + '%';
        // img-a must always appear full-width; undo the clip scaling
        imgA.style.width = (10000 / pct) + '%';
      };

      let dragging = false;

      // Named handlers so we can remove them on close — prevents listener accumulation (#24)
      const onMouseMove = e => {
        if (!dragging) return;
        const rect = container.getBoundingClientRect();
        setPos(((e.clientX - rect.left) / rect.width) * 100);
      };
      const onMouseUp = () => { dragging = false; };
      const onTouchMove = e => {
        if (!dragging) return;
        const rect = container.getBoundingClientRect();
        setPos(((e.touches[0].clientX - rect.left) / rect.width) * 100);
      };
      const onTouchEnd = () => { dragging = false; };

      const cleanup = () => {
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        document.removeEventListener('touchmove', onTouchMove);
        document.removeEventListener('touchend', onTouchEnd);
        document.removeEventListener('keydown', onEsc);
        overlay.remove();
      };

      divider.addEventListener('mousedown', e => { dragging = true; e.preventDefault(); });
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);

      // Touch support
      divider.addEventListener('touchstart', e => { dragging = true; e.preventDefault(); }, { passive: false });
      document.addEventListener('touchmove', onTouchMove, { passive: true });
      document.addEventListener('touchend', onTouchEnd);

      const onEsc = e => { if (e.key === 'Escape') cleanup(); };
      document.addEventListener('keydown', onEsc);
      overlay.addEventListener('click', e => { if (e.target === overlay) cleanup(); });
      overlay.querySelector('button').onclick = cleanup;

      this.compareFrameIndices = [];
    },

    // ── Heatmap ───────────────────────────────────────────────────────────
    heatmapIntensity(count) {
      if (!count) return 'bg-slate-800';
      if (count < 50)  return 'bg-emerald-900';
      if (count < 200) return 'bg-emerald-700';
      if (count < 500) return 'bg-emerald-500';
      return 'bg-emerald-300';
    },

    // Build a 52-week × 7-day grid from daily stats data [{date, count}]
    buildHeatmapGrid(dailyStats) {
      const map = {};
      for (const row of (dailyStats || [])) map[row.date] = row.count;
      const grid = []; // array of weeks (each week = array of 7 day objects)
      const today = new Date();
      today.setHours(0,0,0,0);
      // Start 52 weeks back, on the most recent Sunday
      const start = new Date(today);
      start.setDate(start.getDate() - 52 * 7 - start.getDay());
      const cursor = new Date(start);
      let week = [];
      while (cursor <= today) {
        const iso = cursor.toISOString().slice(0, 10);
        week.push({ date: iso, count: map[iso] || 0 });
        if (week.length === 7) { grid.push(week); week = []; }
        cursor.setDate(cursor.getDate() + 1);
      }
      if (week.length) grid.push(week);
      return grid;
    },

    // ── Scrubber quality filter ───────────────────────────────────────────
    async setScrubberFilter(filter) {
      this.scrubberQualityFilter = filter;
      const id = this.activeProject?.id;
      if (!id) return;
      let url = `/api/projects/${id}/frames?fields=id,captured_at&limit=500`;
      if (filter === 'dark') url += '&is_dark=true';
      else if (filter === 'blurry') url += '&is_blurry=true';
      else if (filter === 'bookmarked') url += '&bookmarked=true';
      const data = await this.api(url);
      if (data) {
        this.activeProjectFrames = data;
        this.scrubberIndex = 0;
        this.updateScrubberFrame();
      }
    },

    // ── Interval analyzer ────────────────────────────────────────────────
    async runIntervalAnalyzer() {
      const id = this.activeProject?.id;
      if (!id) return;
      const secs = this.intervalAnalyzer.targetMinutes * 60;
      const fps = this.intervalAnalyzer.targetFps;
      const data = await this.api(
        `/api/projects/${id}/frames/analyze-interval?target_duration_seconds=${secs}&target_fps=${fps}`
      );
      if (data) this.intervalAnalyzer.result = data;
    },

    // ── Render ETA formatting ────────────────────────────────────────────
    formatEta(etaSeconds) {
      if (!etaSeconds || etaSeconds <= 0) return null;
      if (etaSeconds < 60) return `${etaSeconds}s`;
      if (etaSeconds < 3600) return `${Math.round(etaSeconds / 60)}m`;
      return `${Math.round(etaSeconds / 3600)}h`;
    },

    // ── Render queue drag-to-reorder ─────────────────────────────────────
    onRenderDragStart(renderId) {
      this.dragRenderFrom = renderId;
    },

    async onRenderDrop(targetRenderId) {
      if (!this.dragRenderFrom || this.dragRenderFrom === targetRenderId) return;
      // Find positions in allRenders
      const pending = this.allRenders.filter(r => r.status === 'pending');
      const fromIdx = pending.findIndex(r => r.id === this.dragRenderFrom);
      const toIdx   = pending.findIndex(r => r.id === targetRenderId);
      if (fromIdx < 0 || toIdx < 0) return;

      // Assign priorities: highest index = highest priority (10 down to 1)
      const reordered = [...pending];
      const [moved] = reordered.splice(fromIdx, 1);
      reordered.splice(toIdx, 0, moved);
      // Assign descending priorities
      const updates = reordered.map((r, i) => ({
        id: r.id, priority: reordered.length - i,
      }));
      await Promise.all(updates.map(u =>
        this.api(`/api/renders/${u.id}/priority?priority=${u.priority}`, 'PUT')
      ));
      await this.loadRendersQueue();
      this.dragRenderFrom = null;
    },

    // ── Per-project notification mute ────────────────────────────────────
    isProjectMuted(projectId) {
      return this.mutedProjectIds.includes(projectId);
    },

    async toggleProjectMute(projectId) {
      let newList;
      if (this.mutedProjectIds.includes(projectId)) {
        newList = this.mutedProjectIds.filter(id => id !== projectId);
        this.toast('Notifications unmuted for this project');
      } else {
        newList = [...this.mutedProjectIds, projectId];
        this.toast('Notifications muted for this project');
      }
      this.mutedProjectIds = newList;
      await this.api('/api/settings', 'PUT', { muted_project_ids: newList });
    },

    // ── Pinned / favourite projects ──────────────────────────────────────
    async togglePin(project) {
      const wasPinned = project.is_pinned;
      const method = wasPinned ? 'DELETE' : 'POST';
      const r = await this.api(`/api/projects/${project.id}/pin`, method);
      if (r !== null) {
        project.is_pinned = !wasPinned;
        this.toast(wasPinned ? 'Project unpinned' : 'Project pinned to top');
      }
    },

    // ── Render presets ────────────────────────────────────────────────────
    async loadPresets() {
      const data = await this.api('/api/presets');
      if (data) this.renderPresets = data;
    },

    async applyPreset(preset) {
      this.renderFramerate = preset.framerate;
      this.renderResolution = preset.resolution;
      this.renderQuality = preset.quality;
      this.renderFlicker = preset.flicker_reduction;
      this.renderFrameBlend = !!preset.frame_blend;
      this.renderStabilize = !!preset.stabilize;
      this.renderColorGrade = preset.color_grade;
      this.toast(`Preset "${preset.name}" applied`);
    },

    async saveRenderPreset() {
      const name = prompt('Preset name:');
      if (!name) return;
      const payload = {
        name,
        framerate: this.renderFramerate,
        resolution: this.renderResolution,
        quality: this.renderQuality,
        flicker_reduction: this.renderFlicker,
        frame_blend: this.renderFrameBlend,
        stabilize: this.renderStabilize,
        color_grade: this.renderColorGrade,
      };
      const r = await this.api('/api/presets', 'POST', payload);
      if (r) {
        this.renderPresets.push(r);
        this.toast(`Preset "${name}" saved`);
      }
    },

    async deletePreset(presetId) {
      if (!await this.confirm('Delete this preset?', 'Delete Preset')) return;
      await this.api(`/api/presets/${presetId}`, 'DELETE');
      this.renderPresets = this.renderPresets.filter(p => p.id !== presetId);
      this.toast('Preset deleted');
    },

    // ── Historical range validation / NVR recording range snapping ────────
    async validateHistoricalRange() {
      const cameraId = this.form.camera_id;
      if (!cameraId || this.form.project_type !== 'historical') return;
      const data = await this.api(`/api/cameras/${cameraId}/recording-range`);
      if (!data || !data.available) return;

      let changed = false;
      if (this.form.start_date && data.earliest) {
        const startMs  = new Date(this.form.start_date).getTime();
        const earliest = new Date(data.earliest).getTime();
        if (startMs < earliest) {
          // Snap to earliest available
          this.form.start_date = new Date(earliest).toISOString().slice(0, 16);
          this.toast(`Start date snapped to earliest NVR recording: ${new Date(earliest).toLocaleString()}`, 'warning');
          changed = true;
        }
      }
      if (this.form.end_date && data.latest) {
        const endMs  = new Date(this.form.end_date).getTime();
        const latest = new Date(data.latest).getTime();
        if (endMs > latest) {
          this.form.end_date = new Date(latest).toISOString().slice(0, 16);
          this.toast(`End date snapped to latest NVR recording: ${new Date(latest).toLocaleString()}`, 'warning');
          changed = true;
        }
      }
      if (!changed) {
        this.toast('Date range is within available NVR recordings', 'success');
      }
    },

    // ── Renders ───────────────────────────────────────────────────────────
    async triggerRender(projectId, framerate = 30, resolution = '1920x1080', label = null, renderType = null) {
      const payload = {
        project_id: projectId,
        framerate,
        resolution,
        render_type: renderType || 'manual',
        quality: this.renderQuality,
        flicker_reduction: this.renderFlicker,
        frame_blend: this.renderFrameBlend,
        stabilize: this.renderStabilize,
        color_grade: this.renderColorGrade,
      };
      if (label) payload.label = label;
      if (!renderType && this.rangeStart && this.rangeEnd) {
        payload.render_type = 'range';
        payload.range_start = this.rangeStart;
        payload.range_end = this.rangeEnd;
      }
      const data = await this.api('/api/renders', 'POST', payload);
      if (data) {
        const dur = data.estimated_duration_seconds || 0;
        const fc  = data.frame_count || 0;
        this.lastRenderEstimate = `~${dur}s video · ${fc} frames`;
        this.toast(`Render queued — est. ${dur}s video`);
        await this.loadDetailTab('renders');
      }
    },

    async exportGif() {
      const id = this.activeProject.id;
      this.gifJobStatus = 'pending';
      await this.api(`/api/projects/${id}/gif`, 'POST');
      let gifPollCount = 0;
      const GIF_MAX_POLLS = 150;  // 5 minutes at 2s intervals (#26)
      this.gifPollTimer = setInterval(async () => {
        gifPollCount++;
        if (gifPollCount > GIF_MAX_POLLS) {
          clearInterval(this.gifPollTimer);
          this.gifPollTimer = null;
          this.gifJobStatus = 'error';
          this.toast('GIF export timed out', 'error');
          return;
        }
        const data = await this.api(`/api/projects/${id}/gif/status`);
        if (data) {
          this.gifJobStatus = data.status;
          if (data.status === 'done' || data.status === 'error') {
            clearInterval(this.gifPollTimer);
            this.gifPollTimer = null;
            if (data.status === 'done') this.toast('GIF ready — click to download');
            else this.toast('GIF export failed: ' + (data.error || ''), 'error');
          }
        }
      }, 2000);
    },

    async deleteRender(renderId) {
      if (!await this.confirm('Delete this render file?', 'Delete Render')) return;
      await this.api(`/api/renders/${renderId}`, 'DELETE');
      this.allRenders = this.allRenders.filter(r => r.id !== renderId);
      if (this.view === 'project_detail') await this.loadDetailTab('renders');
      this.toast('Render deleted');
    },

    async openRendersQueue() {
      this.view = 'renders_queue';
      await this.loadRendersQueue();
    },

    async loadRendersQueue() {
      const data = await this.api('/api/renders');
      if (data) this.allRenders = data;
    },

    renderStatusClass(status) {
      return {
        pending:   'text-yellow-400',
        rendering: 'text-blue-400 animate-pulse',
        done:      'text-emerald-400',
        error:     'text-red-400',
      }[status] || 'text-slate-400';
    },

    // ── Shared speed buttons builder (FE9 — DRY) ─────────────────────────
    _buildSpeedButtons(videoSelector, btnClass) {
      return [0.5, 1, 2, 4].map(r =>
        `<button onclick="[...document.querySelectorAll('${videoSelector}')].forEach(v=>v.playbackRate=${r});[...document.querySelectorAll('.${btnClass}')].forEach(b=>b.classList.remove('bg-blue-600','text-white'));this.classList.add('bg-blue-600','text-white')"
                 class="${btnClass} text-sm px-3 py-1 rounded ${r===1?'bg-blue-600 text-white':'bg-slate-700 text-slate-300'} hover:bg-blue-500 transition">${r}×</button>`
      ).join('');
    },

    // ── Video player ──────────────────────────────────────────────────────
    openVideoPlayer(renderId) {
      const url = `/api/renders/${renderId}/download`;
      const overlay = document.createElement('div');
      overlay.id = 'video-overlay';
      overlay.className = 'fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/90';
      overlay.innerHTML = `
        <div class="relative w-full max-w-5xl px-4">
          <button id="video-close-btn"
                  class="fixed top-4 right-4 z-60 w-10 h-10 flex items-center justify-center rounded-full bg-slate-800 hover:bg-red-700 text-white text-xl font-bold shadow-lg transition"
                  title="Close (Esc)">×</button>
          <div id="video-esc-hint"
               class="absolute top-2 left-1/2 -translate-x-1/2 text-xs text-white/70 bg-black/50 px-3 py-1 rounded-full pointer-events-none transition-opacity duration-1000">
            Press Esc to close
          </div>
          <video id="overlay-video" src="${url}" controls preload="metadata" loop muted playsinline
                 class="w-full rounded-xl max-h-[80vh] bg-black"></video>
          <div class="flex items-center justify-between mt-3 gap-4">
            <div class="flex gap-2">
              ${this._buildSpeedButtons('#overlay-video', 'speed-btn')}
            </div>
            <button id="video-close-btn2"
                    class="text-slate-400 hover:text-white text-sm bg-slate-800 px-4 py-2 rounded-lg transition">
              Close [Esc]
            </button>
          </div>
        </div>`;

      // Named handlers for cleanup (FE5)
      const onBgClick = e => { if (e.target === overlay) cleanup(); };
      const onEsc = e => { if (e.key === 'Escape') cleanup(); };
      const cleanup = () => {
        overlay.removeEventListener('click', onBgClick);
        document.removeEventListener('keydown', onEsc);
        overlay.remove();
      };

      overlay.addEventListener('click', onBgClick);
      document.body.appendChild(overlay);
      document.addEventListener('keydown', onEsc);
      document.getElementById('video-close-btn').onclick = cleanup;
      document.getElementById('video-close-btn2').onclick = cleanup;
      document.getElementById('overlay-video').play();
      setTimeout(() => {
        const hint = document.getElementById('video-esc-hint');
        if (hint) hint.style.opacity = '0';
      }, 3000);
    },

    // ── Render comparison ─────────────────────────────────────────────────
    toggleCompareRender(render) {
      const idx = this.compareRenders.findIndex(r => r.id === render.id);
      if (idx >= 0) {
        this.compareRenders.splice(idx, 1);
      } else if (this.compareRenders.length < 2) {
        this.compareRenders.push(render);
      } else {
        this.toast('Select at most 2 renders to compare');
      }
    },

    openComparison() {
      if (this.compareRenders.length !== 2) return;
      const [a, b] = this.compareRenders;
      const urlA = `/api/renders/${a.id}/download`;
      const urlB = `/api/renders/${b.id}/download`;
      const overlay = document.createElement('div');
      overlay.id = 'compare-overlay';
      overlay.className = 'fixed inset-0 z-50 flex flex-col bg-black/95 p-4';

      // Named handlers for cleanup (FE5)
      const onBgClick = e => { if (e.target === overlay) cleanup(); };
      const onEsc = e => { if (e.key === 'Escape') cleanup(); };
      const cleanup = () => {
        overlay.removeEventListener('click', onBgClick);
        document.removeEventListener('keydown', onEsc);
        overlay.remove();
      };

      overlay.innerHTML = `
        <div class="flex items-center justify-between mb-3">
          <div class="flex gap-2">
            ${this._buildSpeedButtons('#cmp-a, #cmp-b', 'cspeed-btn')}
            <button id="cmp-playpause"
                    class="text-sm px-4 py-1 rounded bg-slate-700 text-slate-300 hover:bg-slate-500 transition ml-2">Play/Pause</button>
          </div>
          <button id="cmp-close-btn"
                  class="text-slate-400 hover:text-white text-sm bg-slate-800 px-4 py-2 rounded-lg transition">Close [Esc]</button>
        </div>
        <div class="flex flex-col sm:flex-row flex-1 gap-2 min-h-0">
          <div class="flex-1 flex flex-col min-w-0">
            <p class="text-xs text-slate-400 mb-1 truncate">${a.label || 'Render #' + a.id} — ${a.framerate}fps</p>
            <video id="cmp-a" src="${urlA}" preload="metadata" loop muted playsinline
                   class="flex-1 w-full object-contain bg-black rounded-lg min-h-0"></video>
          </div>
          <div class="flex-1 flex flex-col min-w-0">
            <p class="text-xs text-slate-400 mb-1 truncate">${b.label || 'Render #' + b.id} — ${b.framerate}fps</p>
            <video id="cmp-b" src="${urlB}" preload="metadata" loop muted playsinline
                   class="flex-1 w-full object-contain bg-black rounded-lg min-h-0"></video>
          </div>
        </div>`;

      overlay.addEventListener('click', onBgClick);
      document.body.appendChild(overlay);
      document.addEventListener('keydown', onEsc);
      document.getElementById('cmp-close-btn').onclick = cleanup;
      document.getElementById('cmp-playpause').onclick = () => {
        const va2 = document.getElementById('cmp-a'), vb2 = document.getElementById('cmp-b');
        if (va2) { va2.paused ? va2.play() : va2.pause(); }
        if (vb2) { vb2.paused ? vb2.play() : vb2.pause(); }
      };
      // Sync seek: when one video seeked, sync the other
      const sync = (src, dst) => src.addEventListener('seeked', () => { dst.currentTime = src.currentTime; });
      const va = document.getElementById('cmp-a'), vb = document.getElementById('cmp-b');
      sync(va, vb); sync(vb, va);
      va.play(); vb.play();
    },

    // ── Timeline bar chart helpers ────────────────────────────────────────
    timelineBarHeight(count) {
      return Math.round((count / this.timelineMaxCount) * 100);
    },

    timelineLabel(hour) {
      // hour = "2024-01-01T14:00:00" → "14h"
      const h = hour.slice(11, 13);
      return `${parseInt(h)}h`;
    },

    // ── Resolution validation (FE10) ─────────────────────────────────────
    _resolutionPattern: /^\d+x\d+$/,

    validateResolution() {
      if (!this._resolutionPattern.test(this.renderResolution)) {
        this.toast('Resolution must be in WxH format (e.g. 1920x1080)', 'error');
        this.renderResolution = '1920x1080';
      }
    },

    // ── Render estimate ───────────────────────────────────────────────────
    lastRenderEstimate: null,

    updateRenderEstimate(framerate = 30) {
      const fc = this.activeProject?.frame_count || 0;
      const dur = Math.round(fc / framerate);
      this.lastRenderEstimate = fc > 0
        ? `~${dur}s video · ${fc} frames`
        : 'No frames yet';
    },

    // ── Render auto-refresh (UX10) ────────────────────────────────────────
    renderAutoRefresh: false,
    _renderAutoRefreshTimer: null,

    toggleRenderAutoRefresh() {
      this.renderAutoRefresh = !this.renderAutoRefresh;
      if (this.renderAutoRefresh) {
        this._renderAutoRefreshTimer = setInterval(() => this.loadRendersQueue(), 7000);
      } else {
        clearInterval(this._renderAutoRefreshTimer);
        this._renderAutoRefreshTimer = null;
      }
    },

    // ── Custom confirm dialog (UX7) ───────────────────────────────────────
    _confirmResolve: null,
    showConfirmModal: false,
    confirmMessage: '',
    confirmTitle: '',

    async confirm(message, title = 'Confirm') {
      this.confirmMessage = message;
      this.confirmTitle = title;
      this.showConfirmModal = true;
      return new Promise(resolve => { this._confirmResolve = resolve; });
    },

    _confirmYes() {
      this.showConfirmModal = false;
      if (this._confirmResolve) { this._confirmResolve(true); this._confirmResolve = null; }
    },

    _confirmNo() {
      this.showConfirmModal = false;
      if (this._confirmResolve) { this._confirmResolve(false); this._confirmResolve = null; }
    },

    // ── Undo delete (UX1) ─────────────────────────────────────────────────
    _undoQueue: [],  // { id, type, data, timer }

    _pushUndo(item) {
      this._undoQueue.push(item);
      const undoId = item.id;
      this.toast(`${item.label} deleted — Undo?`, 'info', null, undoId);
      item.timer = setTimeout(() => {
        // Grace period expired — execute real delete
        item.doDelete();
        this._undoQueue = this._undoQueue.filter(u => u.id !== undoId);
      }, 5000);
    },

    undoDelete(undoId) {
      const item = this._undoQueue.find(u => u.id === undoId);
      if (!item) return;
      clearTimeout(item.timer);
      this._undoQueue = this._undoQueue.filter(u => u.id !== undoId);
      item.doRestore();
      this.dismissToastsForUndo(undoId);
      this.toast('Delete undone', 'success');
    },

    dismissToastsForUndo(undoId) {
      this.toasts = this.toasts.filter(t => t.undoId !== undoId);
    },

    // ── Dirty form tracking (UX2) ─────────────────────────────────────────
    _formOriginal: null,

    get formIsDirty() {
      if (!this._formOriginal) return false;
      return JSON.stringify(this.form) !== JSON.stringify(this._formOriginal);
    },

    // ── Frame pagination (FE3) ────────────────────────────────────────────
    framePageSize: 100,
    framePage: 0,

    get pagedFrames() {
      const start = this.framePage * this.framePageSize;
      return this.activeProjectFrames.slice(start, start + this.framePageSize);
    },

    get frameTotalPages() {
      return Math.max(1, Math.ceil(this.activeProjectFrames.length / this.framePageSize));
    },

    // ── Batch progress tracking (UX4) ─────────────────────────────────────
    batchProgress: null,  // null | { done: N, total: N, label: string }

    async _runBatch(ids, label, fn) {
      this.batchProgress = { done: 0, total: ids.length, label };
      const results = [];
      for (const id of ids) {
        const r = await fn(id);
        results.push(r);
        this.batchProgress = { done: results.length, total: ids.length, label };
      }
      this.batchProgress = null;
      return results;
    },

    // ── Historical date range helpers ─────────────────────────────────────
    _toLocalISO(date) {
      // Returns a datetime-local string (YYYY-MM-DDTHH:MM) in local time
      const pad = n => String(n).padStart(2, '0');
      return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    },

    applyHistoricalPreset(preset) {
      this.historicalPresetLabel = preset.label;
      if (preset.custom) return; // let user fill manually
      const now = new Date();
      let start, end;
      if (preset.hours) {
        end = now;
        start = new Date(now.getTime() - preset.hours * 3600000);
      } else if (preset.week === 'current') {
        const day = now.getDay() || 7; // Mon=1
        start = new Date(now); start.setDate(now.getDate() - day + 1); start.setHours(0,0,0,0);
        end = now;
      } else if (preset.week === 'prev') {
        const day = now.getDay() || 7;
        end = new Date(now); end.setDate(now.getDate() - day); end.setHours(23,59,0,0);
        start = new Date(end); start.setDate(end.getDate() - 6); start.setHours(0,0,0,0);
      } else if (preset.month === 'current') {
        start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0);
        end = now;
      } else if (preset.month === 'prev') {
        end = new Date(now.getFullYear(), now.getMonth(), 0, 23, 59);
        start = new Date(end.getFullYear(), end.getMonth(), 1, 0, 0);
      }
      this.form.start_date = this._toLocalISO(start);
      this.form.end_date = this._toLocalISO(end);
    },

    formatHistoricalRange(startStr, endStr) {
      if (!startStr || !endStr) return '';
      const s = new Date(startStr), e = new Date(endStr);
      const diffMs = e - s;
      if (diffMs <= 0) return 'Invalid range';
      const hours = Math.round(diffMs / 3600000);
      const days = Math.floor(hours / 24);
      const remHours = hours % 24;
      const parts = [];
      if (days > 0) parts.push(`${days}d`);
      if (remHours > 0) parts.push(`${remHours}h`);
      return `${s.toLocaleString()} → ${e.toLocaleString()} (${parts.join(' ') || '<1h'})`;
    },

    // ── Create / Edit project form ────────────────────────────────────────
    openCreateForm() {
      this.formMode = 'create';
      this.form = {
        name: '', camera_id: '', project_type: 'live', interval_seconds: 60,
        capture_mode: 'continuous', use_luminance_check: false, luminance_threshold: 15,
        schedule_start_time: '07:00', schedule_end_time: '17:00',
        schedule_days: '1,2,3,4,5',
        auto_render_daily: false, auto_render_weekly: false, auto_render_monthly: false,
        retention_days: 0, max_frames: null, width: null, height: null,
        use_motion_filter: false, motion_threshold: 5,
        solar_noon_window_minutes: 30,
      };
      this.selectedTemplate = null;
      this.previewUrl = null;
      this.historicalPresetLabel = '';
      this.view = 'create_project';
    },

    openEditForm(project) {
      this.formMode = 'edit';
      this.form = { ...project };
      this._formOriginal = { ...project };  // for dirty tracking (UX2)
      this.view = 'create_project';
    },

    applyTemplate(templateId) {
      const tmpl = this.templates.find(t => t.id === parseInt(templateId));
      if (!tmpl) return;
      const preserved = { name: this.form.name, camera_id: this.form.camera_id };
      this.form = {
        ...tmpl,
        ...preserved,
        project_type: this.form.project_type || 'live',
      };
    },

    async saveAsTemplate() {
      const name = prompt('Template name:');
      if (!name) return;
      const payload = { ...this.form, name };
      delete payload.id;
      delete payload.camera_id;
      const data = await this.api('/api/templates', 'POST', payload);
      if (data) {
        await this.loadTemplates();
        this.toast('Template saved');
      }
    },

    _submitting: false,

    _validateForm() {
      // Client-side validation before API call (FE4)
      if (!this.form.name || !this.form.name.trim()) {
        this.toast('Project name is required', 'error'); return false;
      }
      if (!this.form.camera_id) {
        this.toast('Please select a camera', 'error'); return false;
      }
      const interval = parseInt(this.form.interval_seconds);
      if (!interval || interval < 1) {
        this.toast('Interval must be at least 1 second', 'error'); return false;
      }
      return true;
    },

    async submitForm() {
      if (this._submitting) return;  // prevent double-submit (#27)
      if (!this._validateForm()) return;
      this._submitting = true;
      try {
        if (this.formMode === 'create') {
          const data = await this.api('/api/projects', 'POST', this.form);
          if (data) {
            await this.loadProjects();
            this.toast('Project created');
            this.view = 'dashboard';
          }
        } else {
          const data = await this.api(`/api/projects/${this.form.id}`, 'PUT', this.form);
          if (data) {
            await this.loadProjects();
            this.toast('Project updated');
            this.view = 'dashboard';
          }
        }
      } finally {
        this._submitting = false;
      }
    },

    onCameraSelected() {
      this.startPreviewPolling();
    },

    startPreviewPolling() {
      this.stopPreviewPolling();
      if (!this.form.camera_id || this.form.project_type === 'historical') return;
      const poll = () => {
        this.previewUrl = `/api/cameras/${this.form.camera_id}/preview?t=${Date.now()}`;
      };
      poll();
      this.previewTimer = setInterval(poll, 5000);
    },

    stopPreviewPolling() {
      if (this.previewTimer) { clearInterval(this.previewTimer); this.previewTimer = null; }
    },

    setIntervalPreset(seconds) { this.form.interval_seconds = seconds; },

    // ── Settings ──────────────────────────────────────────────────────────
    settingsData: {},
    nvrTestResult: null,
    nvrTesting: false,

    async openSettings() {
      this.view = 'settings';
      const data = await this.api('/api/settings');
      if (data) {
        this.settingsData = data;
        this.loadTheme(data.dark_mode);
      }
    },

    async saveSettings() {
      const SETTINGS_FIELDS = [
        'webhook_url','disk_warning_threshold_gb','timestamp_burn_in','default_framerate',
        'render_poll_interval_seconds','protect_host','protect_port','protect_verify_ssl',
        'latitude','longitude','tz','dark_mode','maintenance_hour','maintenance_minute',
        'nvr_reconnect_backoff_seconds',
      ];
      const payload = Object.fromEntries(
        SETTINGS_FIELDS.map(k => [k, this.settingsData[k] !== undefined ? this.settingsData[k] : null])
      );
      // muted_project_ids is stored as a JSON string in the DB — parse before sending
      const rawMuted = this.settingsData['muted_project_ids'];
      payload['muted_project_ids'] = rawMuted
        ? (Array.isArray(rawMuted) ? rawMuted : JSON.parse(rawMuted))
        : [];
      const data = await this.api('/api/settings', 'PUT', payload);
      if (data) {
        this.settingsData = data;
        this.loadTheme(data.dark_mode);
        this.toast('Settings saved');
      }
    },

    async testNvrConnection() {
      this.nvrTesting = true;
      this.nvrTestResult = null;
      const data = await this.api('/api/settings/nvr-test');
      this.nvrTestResult = data;
      this.nvrTesting = false;
    },

    async uploadWatermark(event) {
      const file = event.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      const resp = await fetch('/api/settings/watermark', { method: 'POST', body: form });
      if (resp.ok) {
        const data = await resp.json();
        this.settingsData = { ...this.settingsData, watermark_path: data.watermark_path };
        this.toast('Watermark uploaded');
      } else {
        this.toast('Upload failed', 'error');
      }
    },

    async clearWatermark() {
      if (!confirm('Remove watermark?')) return;
      const resp = await fetch('/api/settings/watermark', { method: 'DELETE' });
      if (resp.ok || resp.status === 204) {
        this.settingsData = { ...this.settingsData, watermark_path: null };
        this.toast('Watermark removed');
      }
    },

    async deleteTemplate(id) {
      if (!confirm('Delete this template?')) return;
      await this.api(`/api/templates/${id}`, 'DELETE');
      await this.loadTemplates();
      this.toast('Template deleted');
    },

    async markAllRead() {
      await this.api('/api/notifications/read', 'PUT', { all: true });
      await this.loadNotifications();
    },

    async deleteNotification(id) {
      await this.api(`/api/notifications/${id}`, 'DELETE');
      this.notifications = this.notifications.filter(n => n.id !== id);
      this.unreadCount = this.notifications.filter(n => !n.is_read).length;
    },

    async clearAllNotifications(readOnly = false) {
      const url = readOnly ? '/api/notifications?read_only=true' : '/api/notifications';
      await this.api(url, 'DELETE');
      await this.loadNotifications();
      this.toast(readOnly ? 'Read notifications cleared' : 'All notifications cleared');
    },

    // ── WebSocket ─────────────────────────────────────────────────────────
    _wsConnecting: false,

    connectWebSocket() {
      // Guard against multiple simultaneous connect attempts (#25)
      if (this._wsConnecting || (this.ws && this.ws.readyState === WebSocket.CONNECTING)) return;
      this._wsConnecting = true;

      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const url = `${proto}://${location.host}/api/ws`;
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        this._wsConnecting = false;
        this.wsRetries = 0;
        console.log('[WS] connected');
      };

      this.ws.onmessage = (e) => {
        try { this.handleWsEvent(JSON.parse(e.data)); } catch (err) {
          console.error('[WS] message handler error:', err, e.data);  // (#14)
        }
      };

      this.ws.onclose = () => {
        this._wsConnecting = false;
        if (this.wsRetries < this.wsMaxRetries) {
          const delay = Math.min(1000 * Math.pow(2, this.wsRetries), 30000);
          this.wsRetries++;
          setTimeout(() => this.connectWebSocket(), delay);
        } else {
          console.warn('[WS] fallback to polling');
          this.startPolling();
        }
      };
    },

    handleWsEvent(msg) {
      switch (msg.event) {
        case 'capture_event': {
          const p = this.projects.find(p => p.id === msg.project_id);
          if (p) { p.frame_count = msg.frame_count; }
          if (this.activeProject?.id === msg.project_id) {
            this.activeProject.frame_count = msg.frame_count;
          }
          break;
        }
        case 'capture_batch': {
          for (const upd of (msg.updates || [])) {
            const p = this.projects.find(p => p.id === upd.project_id);
            if (p) { p.frame_count = upd.frame_count; }
            if (this.activeProject?.id === upd.project_id) {
              this.activeProject.frame_count = upd.frame_count;
            }
          }
          break;
        }
        case 'render_progress': {
          // Update both the per-project tab and the global queue view
          const r = this.activeProjectRenders.find(r => r.id === msg.render_id);
          if (r) { r.progress_pct = msg.progress_pct; r.status = 'rendering'; }
          const rq = this.allRenders.find(r => r.id === msg.render_id);
          if (rq) { rq.progress_pct = msg.progress_pct; rq.status = 'rendering'; }
          break;
        }
        case 'render_complete': {
          const r = this.activeProjectRenders.find(r => r.id === msg.render_id);
          if (r) { r.status = msg.status; r.progress_pct = msg.status === 'done' ? 100 : r.progress_pct; }
          const rq = this.allRenders.find(r => r.id === msg.render_id);
          if (rq) { rq.status = msg.status; if (msg.status === 'done') rq.progress_pct = 100; }
          if (this.view === 'project_detail') this.loadDetailTab('renders');
          if (this.view === 'renders_queue') this.loadRendersQueue();
          const projId = r?.project_id ?? rq?.project_id ?? msg.project_id;
          const pName = this.projects.find(p => p.id === projId)?.name || 'project';
          const label = msg.status === 'done' ? `Render done: ${pName}` : `Render failed: ${pName}`;
          this.toast(label, msg.status === 'done' ? 'success' : 'error', projId);
          break;
        }
        case 'extraction_progress': {
          const p = this.projects.find(p => p.id === msg.project_id);
          if (p) {
            if (msg.progress_pct < 0 || msg.error) {
              // Extraction failed
              p.status = 'error';
              p._extraction_pct = null;
              this.toast(`Extraction failed: ${msg.error || 'Unknown error'}`, 'error', p.id);
            } else if (msg.progress_pct >= 100) {
              p.status = 'completed';
              p._extraction_pct = null;
              this.toast(`Extraction complete: ${p.name} (${msg.frames} frames)`, 'success', p.id);
            } else {
              p.frame_count = msg.frames || 0;
              p._extraction_pct = msg.progress_pct;
              if (msg.current && msg.total_expected) {
                p._extraction_detail = `${msg.current}/${msg.total_expected}`;
              }
            }
          }
          break;
        }
        case 'disk_update': {
          this.diskSpace.free_gb = msg.free_gb;
          if (msg.total_gb) this.diskSpace.total_gb = msg.total_gb;
          break;
        }
        case 'notification': {
          this.notifications.unshift({
            event: msg.event, level: msg.level, message: msg.message,
            is_read: false, created_at: msg.timestamp,
          });
          this.unreadCount++;
          this.toast(msg.message, msg.level === 'error' ? 'error' : 'info');
          break;
        }
        case 'nvr_status': {
          if (this.systemStatus) this.systemStatus.nvr = msg;
          break;
        }
        case 'project_status_change': {
          const p = this.projects.find(p => p.id === msg.project_id);
          if (p) {
            p.status = msg.status;
            if (msg.reason) p._status_reason = msg.reason;
          }
          if (msg.status === 'paused_error') {
            const name = p?.name || `Project #${msg.project_id}`;
            this.toast(`${name}: ${msg.reason || 'Auto-paused'}`, 'error', msg.project_id);
          }
          break;
        }
      }
    },

    // ── Centralized timer cleanup (FE1) ───────────────────────────────────
    clearAllPollingTimers() {
      if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
      if (this._renderPollTimer) { clearInterval(this._renderPollTimer); this._renderPollTimer = null; }
      if (this._cameraGridTimer) { clearInterval(this._cameraGridTimer); this._cameraGridTimer = null; }
      if (this.gifPollTimer) { clearInterval(this.gifPollTimer); this.gifPollTimer = null; }
      if (this.previewTimer) { clearInterval(this.previewTimer); this.previewTimer = null; }
      if (this._renderAutoRefreshTimer) { clearInterval(this._renderAutoRefreshTimer); this._renderAutoRefreshTimer = null; }
      if (this._systemStatusTimer) { clearInterval(this._systemStatusTimer); this._systemStatusTimer = null; }
    },

    // ── Mobile tap-to-preview (UX8) ───────────────────────────────────────
    tapPreviewFrame: null,

    onFrameTap(frame) {
      // On touch devices, show a larger preview overlay
      if (!window.matchMedia('(pointer: coarse)').matches) return;
      if (this.tapPreviewFrame?.id === frame.id) {
        this.tapPreviewFrame = null;
        return;
      }
      this.tapPreviewFrame = frame;
    },

    closeTapPreview() {
      this.tapPreviewFrame = null;
    },

    _pollTimer: null,
    startPolling() {
      this._pollTimer = setInterval(() => this.loadAll(), 30000);
    },

    // Poll active renders every 2s when WS is not available
    _renderPollTimer: null,
    get hasActiveRender() {
      return this.activeProjectRenders.some(r => r.status === 'rendering');
    },

    // ── Toast ─────────────────────────────────────────────────────────────
    _toastCounter: 0,

    toast(message, type = 'success', projectId = null, undoId = null) {
      const id = ++this._toastCounter;  // monotonic counter avoids Date.now() collisions (FE7)
      this.toasts.push({ id, message, type, projectId, undoId });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, undoId ? 5500 : 4000);
    },

    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    clickToast(t) {
      if (t.undoId) {
        // Undo action toast — trigger undo rather than navigate (UX1)
        this.undoDelete(t.undoId);
        return;
      }
      // Click-to-copy error text (UX6)
      if (t.type === 'error' && navigator.clipboard) {
        navigator.clipboard.writeText(t.message).catch(() => {});
        this.dismissToast(t.id);
        return;
      }
      this.dismissToast(t.id);
      if (t.projectId) {
        const proj = this.projects.find(p => p.id === t.projectId);
        if (proj) this.openProject(proj);
      }
    },

    // ── Keyboard shortcuts ────────────────────────────────────────────────
    initKeyboardShortcuts() {
      window.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        switch (e.key) {
          case 'n': case 'N':
            if (this.view === 'dashboard') { e.preventDefault(); this.openCreateForm(); }
            break;
          case 'Escape':
            if (this.view !== 'dashboard') {
              this.toasts = this.toasts.filter(t => !t.undoId);
              this.view = 'dashboard';
            }
            this.showNotifDropdown = false;
            this.showShortcutHelp = false;
            break;
          case '?':
            this.showShortcutHelp = !this.showShortcutHelp;
            break;
          case 'r': case 'R':
            if (this.view === 'project_detail' && this.activeProject) {
              e.preventDefault();
              this.triggerRender(this.activeProject.id, this.renderFramerate, this.renderResolution);
            }
            break;
          case 'b': case 'B':
            if (this.view === 'project_detail') {
              e.preventDefault();
              const note = prompt('Bookmark note:');
              if (note !== null) this.bookmarkCurrentFrame(note);
            }
            break;
          case 'c': case 'C':
            if (this.view === 'project_detail' && this.detailTab === 'overview') {
              e.preventDefault();
              this.markCompareFrame();
            }
            break;
          // Historical range preset shortcuts: 1=24h, 2=7d, 3=30d (UX9)
          case '1':
            if (this.view === 'project_detail') {
              this.applyHistoricalPreset(this.historicalPresets[0]); e.preventDefault();
            }
            break;
          case '2':
            if (this.view === 'project_detail') {
              this.applyHistoricalPreset(this.historicalPresets[1]); e.preventDefault();
            }
            break;
          case '3':
            if (this.view === 'project_detail') {
              this.applyHistoricalPreset(this.historicalPresets[2]); e.preventDefault();
            }
            break;
          case 'ArrowLeft':
            if (this.view === 'project_detail' && this.activeProjectFrames.length) {
              const step = e.shiftKey ? 10 : 1;
              this.scrubberIndex = Math.max(0, this.scrubberIndex - step);
              this.updateScrubberFrame();
            }
            break;
          case 'ArrowRight':
            if (this.view === 'project_detail' && this.activeProjectFrames.length) {
              const step = e.shiftKey ? 10 : 1;
              this.scrubberIndex = Math.min(this.activeProjectFrames.length - 1, this.scrubberIndex + step);
              this.updateScrubberFrame();
            }
            break;
        }
      });
    },

    // ── Helpers ───────────────────────────────────────────────────────────
    async api(url, method = 'GET', body = null) {
      try {
        const opts = { method, headers: {} };
        if (body && method !== 'GET') {
          opts.headers['Content-Type'] = 'application/json';
          opts.body = JSON.stringify(body);
        }
        const resp = await fetch(url, opts);
        if (resp.status === 204) return true;
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          this.toast(err.detail || 'Request failed', 'error');
          return null;
        }
        return await resp.json();
      } catch (ex) {
        this.toast('Network error', 'error');
        return null;
      }
    },

    formatDate(iso) {
      if (!iso) return '—';
      return new Date(iso).toLocaleString();
    },

    timeSince(iso) {
      if (!iso) return null;
      const secs = (Date.now() - new Date(iso).getTime()) / 1000;
      if (secs < 60) return Math.round(secs) + 's ago';
      if (secs < 3600) return Math.round(secs / 60) + 'm ago';
      if (secs < 86400) return Math.round(secs / 3600) + 'h ago';
      return Math.round(secs / 86400) + 'd ago';
    },

    timeSinceBadgeClass(proj) {
      if (!proj.last_captured_at || proj.status !== 'active') return 'bg-slate-700 text-slate-400';
      const secs = (Date.now() - new Date(proj.last_captured_at).getTime()) / 1000;
      const threshold = proj.interval_seconds || 60;
      if (secs < threshold * 2) return 'bg-emerald-700/60 text-emerald-300';
      if (secs < threshold * 10) return 'bg-amber-700/60 text-amber-300';
      return 'bg-red-700/60 text-red-300';
    },

    formatBytes(bytes) {
      if (!bytes) return '—';
      if (bytes > 1024 ** 3) return (bytes / 1024 ** 3).toFixed(1) + ' GB';
      if (bytes > 1024 ** 2) return (bytes / 1024 ** 2).toFixed(1) + ' MB';
      return (bytes / 1024).toFixed(0) + ' KB';
    },

    statusDot(status) {
      return {
        active:       'bg-emerald-500 animate-pulse',
        paused:       'bg-yellow-400',
        paused_error: 'bg-red-500',
        error:        'bg-red-500',
        completed:    'bg-blue-400',
        extracting:   'bg-purple-400 animate-pulse',
      }[status] || 'bg-slate-500';
    },
  }));
});
