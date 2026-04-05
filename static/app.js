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
      if (s) this.loadTheme(s.dark_mode);
      else this.loadTheme(true);
      this.initKeyboardShortcuts();
      await this.loadAll();
      this.connectWebSocket();
    },

    loadTheme(darkMode) {
      this.darkMode = darkMode !== false && darkMode !== 0;
      document.documentElement.classList.toggle('dark', this.darkMode);
    },

    toggleDark() {
      this.darkMode = !this.darkMode;
      document.documentElement.classList.toggle('dark', this.darkMode);
      localStorage.setItem('darkMode', String(this.darkMode));
      // Persist immediately — fire-and-forget
      this.api('/api/settings', 'PUT', { dark_mode: this.darkMode });
    },

    async loadAll() {
      await Promise.all([
        this.loadProjects(),
        this.loadCameras(),
        this.loadTemplates(),
        this.loadNotifications(),
        this.loadHealth(),
      ]);
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
            id: proj.camera_id, name: proj.camera_id, is_online: false, projects: [proj]
          };
        }
      }
      return Object.values(map);
    },

    cameraHasError(cam) {
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
      if (!confirm('Delete this project and all its frames? This cannot be undone.')) return;
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
      await Promise.all(this.selectedProjectIds.map(id => this.api(`/api/projects/${id}`, 'PATCH', { status: 'paused' })));
      await this.loadProjects();
      this.toast(`Paused ${this.selectedProjectIds.length} project(s)`);
      this.selectedProjectIds = [];
    },

    async bulkResume() {
      await Promise.all(this.selectedProjectIds.map(id => this.api(`/api/projects/${id}`, 'PATCH', { status: 'active' })));
      await this.loadProjects();
      this.toast(`Resumed ${this.selectedProjectIds.length} project(s)`);
      this.selectedProjectIds = [];
    },

    async bulkDelete() {
      if (!confirm(`Delete ${this.selectedProjectIds.length} project(s)? This cannot be undone.`)) return;
      await Promise.all(this.selectedProjectIds.map(id => this.api(`/api/projects/${id}`, 'DELETE')));
      await this.loadProjects();
      this.toast(`Deleted ${this.selectedProjectIds.length} project(s)`);
      this.selectedProjectIds = [];
    },

    // ── Project detail ────────────────────────────────────────────────────
    async openProject(project) {
      this.activeProject = project;
      this.detailTab = 'overview';
      this.view = 'project_detail';
      this.scrubberIndex = 0;
      this.rangeStart = null;
      this.rangeEnd = null;
      this.gifJobStatus = null;
      if (this.gifPollTimer) { clearInterval(this.gifPollTimer); this.gifPollTimer = null; }
      await this.loadProjectDetail();
    },

    async loadProjectDetail() {
      if (!this.activeProject) return;
      const id = this.activeProject.id;
      const [frames, renders, daily, timeline] = await Promise.all([
        this.api(`/api/projects/${id}/frames?fields=id,captured_at&limit=500`),
        this.api(`/api/projects/${id}/renders`),
        this.api(`/api/projects/${id}/stats/daily`),
        this.api(`/api/projects/${id}/stats/timeline`),
      ]);
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
      if (!confirm('Delete this frame permanently? This cannot be undone.')) return;
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
          <button onclick="document.getElementById('frame-compare-overlay').remove()"
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

      overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
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
      divider.addEventListener('mousedown', e => { dragging = true; e.preventDefault(); });
      document.addEventListener('mousemove', e => {
        if (!dragging) return;
        const rect = container.getBoundingClientRect();
        setPos(((e.clientX - rect.left) / rect.width) * 100);
      });
      document.addEventListener('mouseup', () => { dragging = false; });

      // Touch support
      divider.addEventListener('touchstart', e => { dragging = true; e.preventDefault(); }, { passive: false });
      document.addEventListener('touchmove', e => {
        if (!dragging) return;
        const rect = container.getBoundingClientRect();
        setPos(((e.touches[0].clientX - rect.left) / rect.width) * 100);
      }, { passive: true });
      document.addEventListener('touchend', () => { dragging = false; });

      const esc = e => { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc); } };
      document.addEventListener('keydown', esc);

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
      this.gifPollTimer = setInterval(async () => {
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
      if (!confirm('Delete this render file?')) return;
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

    // ── Video player ──────────────────────────────────────────────────────
    openVideoPlayer(renderId) {
      const url = `/api/renders/${renderId}/download`;
      const overlay = document.createElement('div');
      overlay.id = 'video-overlay';
      overlay.className = 'fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/90';
      overlay.innerHTML = `
        <div class="relative w-full max-w-5xl px-4">
          <!-- Prominent × close button pinned top-right -->
          <button id="video-close-btn"
                  onclick="document.getElementById('video-overlay').remove()"
                  class="fixed top-4 right-4 z-60 w-10 h-10 flex items-center justify-center rounded-full bg-slate-800 hover:bg-red-700 text-white text-xl font-bold shadow-lg transition"
                  title="Close (Esc)">×</button>
          <!-- Esc hint overlaid on video — fades out after 3s -->
          <div id="video-esc-hint"
               class="absolute top-2 left-1/2 -translate-x-1/2 text-xs text-white/70 bg-black/50 px-3 py-1 rounded-full pointer-events-none transition-opacity duration-1000">
            Press Esc to close
          </div>
          <video id="overlay-video" src="${url}" controls preload="metadata" loop muted playsinline
                 class="w-full rounded-xl max-h-[80vh] bg-black"></video>
          <div class="flex items-center justify-between mt-3 gap-4">
            <div class="flex gap-2">
              ${[0.5, 1, 2, 4].map(r =>
                `<button onclick="document.getElementById('overlay-video').playbackRate=${r};[...document.querySelectorAll('.speed-btn')].forEach(b=>b.classList.remove('bg-blue-600','text-white'));this.classList.add('bg-blue-600','text-white')"
                         class="speed-btn text-sm px-3 py-1 rounded ${r===1?'bg-blue-600 text-white':'bg-slate-700 text-slate-300'} hover:bg-blue-500 transition">${r}×</button>`
              ).join('')}
            </div>
            <button onclick="document.getElementById('video-overlay').remove()"
                    class="text-slate-400 hover:text-white text-sm bg-slate-800 px-4 py-2 rounded-lg transition">
              Close [Esc]
            </button>
          </div>
        </div>`;
      overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
      document.body.appendChild(overlay);
      const esc = e => { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc); } };
      document.addEventListener('keydown', esc);
      document.getElementById('overlay-video').play();
      // Fade out the Esc hint after 3s
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
      overlay.innerHTML = `
        <div class="flex items-center justify-between mb-3">
          <div class="flex gap-2">
            ${[0.5, 1, 2, 4].map(r =>
              `<button onclick="[document.getElementById('cmp-a'),document.getElementById('cmp-b')].forEach(v=>v.playbackRate=${r});[...document.querySelectorAll('.cspeed-btn')].forEach(b=>b.classList.remove('bg-blue-600','text-white'));this.classList.add('bg-blue-600','text-white')"
                       class="cspeed-btn text-sm px-3 py-1 rounded ${r===1?'bg-blue-600 text-white':'bg-slate-700 text-slate-300'} hover:bg-blue-500 transition">${r}×</button>`
            ).join('')}
            <button onclick="const va=document.getElementById('cmp-a'),vb=document.getElementById('cmp-b');va.paused?va.play()&&vb.play():va.pause()&&vb.pause()"
                    class="text-sm px-4 py-1 rounded bg-slate-700 text-slate-300 hover:bg-slate-500 transition ml-2">Play/Pause</button>
          </div>
          <button onclick="document.getElementById('compare-overlay').remove()"
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
      overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
      document.body.appendChild(overlay);
      const esc = e => { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc); } };
      document.addEventListener('keydown', esc);
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

    // ── Render estimate ───────────────────────────────────────────────────
    lastRenderEstimate: null,

    updateRenderEstimate(framerate = 30) {
      const fc = this.activeProject?.frame_count || 0;
      const dur = Math.round(fc / framerate);
      this.lastRenderEstimate = fc > 0
        ? `~${dur}s video · ${fc} frames`
        : 'No frames yet';
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
      };
      this.selectedTemplate = null;
      this.previewUrl = null;
      this.historicalPresetLabel = '';
      this.view = 'create_project';
    },

    openEditForm(project) {
      this.formMode = 'edit';
      this.form = { ...project };
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

    async submitForm() {
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
      const data = await this.api('/api/settings', 'PUT', this.settingsData);
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
    connectWebSocket() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const url = `${proto}://${location.host}/api/ws`;
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        this.wsRetries = 0;
        console.log('[WS] connected');
      };

      this.ws.onmessage = (e) => {
        try { this.handleWsEvent(JSON.parse(e.data)); } catch {}
      };

      this.ws.onclose = () => {
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
          const r = this.activeProjectRenders.find(r => r.id === msg.render_id);
          if (r) { r.progress_pct = msg.progress_pct; }
          break;
        }
        case 'render_complete': {
          const r = this.activeProjectRenders.find(r => r.id === msg.render_id);
          if (r) { r.status = msg.status; r.progress_pct = 100; }
          this.loadDetailTab('renders');
          const projId = r?.project_id ?? msg.project_id;
          const pName = this.projects.find(p => p.id === projId)?.name || 'project';
          const label = msg.status === 'done' ? `Render done: ${pName}` : `Render failed: ${pName}`;
          this.toast(label, msg.status === 'done' ? 'success' : 'error', projId);
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
      }
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
    toast(message, type = 'success', projectId = null) {
      const id = Date.now();
      this.toasts.push({ id, message, type, projectId });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 4000);
    },

    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    clickToast(t) {
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
            if (this.view !== 'dashboard') { this.view = 'dashboard'; }
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
