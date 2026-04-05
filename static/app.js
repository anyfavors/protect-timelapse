/* Protect Timelapse — Alpine.js SPA
 * Single global component: timelapseApp()
 * Script must be loaded with <script defer> BEFORE the Alpine CDN tag.
 */

document.addEventListener('alpine:init', () => {
  Alpine.data('timelapseApp', () => ({
    // ── State ─────────────────────────────────────────────────────────────
    view: 'dashboard',        // dashboard | project_detail | create_project | settings
    projects: [],
    cameras: [],
    activeProject: null,
    activeProjectFrames: [],
    activeProjectRenders: [],
    activeProjectBookmarks: [],
    activeProjectDarkFrames: [],
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

    // Render comparison & video player
    compareRenders: [],
    videoPlaybackRate: 1,
    comparisonSyncing: false,

    // Hourly timeline chart
    timelineMaxCount: 1,

    // ── Lifecycle ─────────────────────────────────────────────────────────
    async init() {
      this.loadTheme();
      this.initKeyboardShortcuts();
      await this.loadAll();
      this.connectWebSocket();
    },

    loadTheme() {
      document.documentElement.classList.add('dark');
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

    // ── Project detail ────────────────────────────────────────────────────
    async openProject(project) {
      this.activeProject = project;
      this.detailTab = 'overview';
      this.view = 'project_detail';
      this.scrubberIndex = 0;
      this.rangeStart = null;
      this.rangeEnd = null;
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
      } else if (tab === 'dark_frames') {
        const data = await this.api(`/api/projects/${id}/frames/dark`);
        if (data) this.activeProjectDarkFrames = data;
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
    async triggerRender(projectId, framerate = 30, resolution = '1920x1080', label = null) {
      const payload = { project_id: projectId, framerate, resolution, render_type: 'manual' };
      if (label) payload.label = label;
      if (this.rangeStart && this.rangeEnd) {
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

    async deleteRender(renderId) {
      if (!confirm('Delete this render file?')) return;
      await this.api(`/api/renders/${renderId}`, 'DELETE');
      await this.loadDetailTab('renders');
      this.toast('Render deleted');
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
      // Open in a minimal overlay using a native <video> element injected into the DOM
      const overlay = document.createElement('div');
      overlay.id = 'video-overlay';
      overlay.className = 'fixed inset-0 z-50 flex flex-col items-center justify-center bg-black/90';
      overlay.innerHTML = `
        <div class="relative w-full max-w-5xl px-4">
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
        <div class="flex flex-1 gap-2 min-h-0">
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
      };
      this.selectedTemplate = null;
      this.previewUrl = null;
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
      if (!this.form.camera_id) return;
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

    async openSettings() {
      this.view = 'settings';
      const data = await this.api('/api/settings');
      if (data) this.settingsData = data;
    },

    async saveSettings() {
      const data = await this.api('/api/settings', 'PUT', this.settingsData);
      if (data) { this.settingsData = data; this.toast('Settings saved'); }
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
        case 'render_progress': {
          const r = this.activeProjectRenders.find(r => r.id === msg.render_id);
          if (r) { r.progress_pct = msg.progress_pct; }
          break;
        }
        case 'render_complete': {
          const r = this.activeProjectRenders.find(r => r.id === msg.render_id);
          if (r) { r.status = msg.status; r.progress_pct = 100; }
          this.loadDetailTab('renders');
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
    toast(message, type = 'success') {
      const id = Date.now();
      this.toasts.push({ id, message, type });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 4000);
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
              this.triggerRender(this.activeProject.id);
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
      }[status] || 'bg-slate-500';
    },
  }));
});
