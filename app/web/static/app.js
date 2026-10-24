/* WishPanel UI shell (Alpine.js). All URLs are relative so BASE_URL prefixes work. */
"use strict";

class ApiError extends Error {
  constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; }
}

let csrfToken = "";

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let res;
  try {
    res = await fetch(path, {
      method, headers, credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Network error — is the WishPanel container running?");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const detail = data?.detail;
    let msg = detail;
    if (Array.isArray(detail)) msg = detail.map((d) => d.msg).join("; ");
    else if (detail && typeof detail === "object") msg = detail.message;
    throw new ApiError(res.status, msg || `Request failed (HTTP ${res.status})`, detail);
  }
  return data;
}

const TABS = [
  { id: "discover", label: "Discover" },
  { id: "search", label: "Search" },
  { id: "requests", label: "Requests" },
  { id: "admin", label: "Admin", admin: true },
];

const MEDIA = [
  { id: "comic", label: "Comics" },
  { id: "manga", label: "Manga" },
  { id: "manhwa", label: "Manhwa" },
];

// Admin navigation (SPEC §7). Pages with `schema: true` are rendered from the
// server's settings schema; `custom` pages have their own markup; the rest are
// placeholders for later milestones.
const ADMIN_NAV = [
  { id: "overview", label: "Overview", custom: true },
  { id: "general", label: "General", schema: true },
  { id: "authentication", label: "Authentication", schema: true },
  { id: "users", label: "Users", custom: true },
  { id: "requests-admin", label: "Request queue", href: "#/requests/all" },
  { id: "media-types", label: "Media Types", schema: true },
  { id: "kavita", label: "Library — Kavita", schema: true },
  { id: "kapowarr", label: "Downloader — Kapowarr", schema: true },
  { id: "suwayomi", label: "Downloader — Suwayomi", schema: true },
  { id: "comicvine", label: "Metadata — ComicVine", schema: true },
  { id: "anilist", label: "Metadata — AniList / MangaBaka", schema: true },
  { id: "discover", label: "Discover", schema: true },
  { id: "requests", label: "Requests", schema: true },
  { id: "jobs", label: "Jobs", schema: true },
  { id: "logs", label: "Logs & About", custom: true },
];

const STATUS_LABEL = {
  pending: "Pending approval", approved: "Approved", added: "Added", downloading: "Downloading",
  partial: "Partially available", in_library: "In library", failed: "Failed",
  waiting_for_source: "Waiting for source", waiting_for_downloader: "Waiting for downloader", denied: "Denied",
};
const STATUS_TONE = {
  pending: "warn", approved: "info", added: "info", downloading: "info", partial: "warn",
  in_library: "ok", failed: "bad", waiting_for_source: "warn", waiting_for_downloader: "warn", denied: "bad",
};
const REQUEST_FILTERS = [
  { id: "active", label: "Active" }, { id: "pending", label: "Pending" },
  { id: "waiting_for_source", label: "Waiting for source" }, { id: "partial", label: "Partial" },
  { id: "in_library", label: "In library" }, { id: "failed", label: "Failed" }, { id: "", label: "All" },
];
const BUSY_STATUSES = new Set(["approved"]);

const SERVICE_URL_KEY = { kavita: "kavita.url", kapowarr: "kapowarr.url", suwayomi: "suwayomi.url" };

function normalise(field, value) {
  if (field.type === "list") {
    const raw = Array.isArray(value) ? value : String(value ?? "").split("\n");
    const out = [];
    for (const v of raw) { const t = String(v).trim(); if (t && !out.includes(t)) out.push(t); }
    return out;
  }
  if (field.type === "int") return value === "" || value === null ? null : Number(value);
  if (field.type === "bool") return !!value;
  if (field.type === "url") return String(value ?? "").trim().replace(/\/+$/, "");
  return String(value ?? "").trim();
}

function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

document.addEventListener("alpine:init", () => {
  window.Alpine.data("wishpanel", () => ({
    view: "boot",
    fatal: "",
    error: "",
    busy: false,
    site: { site_name: "WishPanel", version: "", media_types: {} },
    me: null,
    tab: "discover",
    adminPage: "overview",
    admin: null,
    // settings pages
    schema: [],
    values: {},       // as saved on the server (secrets: {set, masked})
    draft: {},        // non-secret edits, keyed by setting key
    secretDraft: {},  // secret edits: "" = untouched in the input
    secretClear: {},  // secret explicitly cleared
    fieldErrors: {},
    pageMsg: null,    // {ok, message} after save / scan / logo
    testResult: {},   // pageId -> {ok, message}
    opts: {},         // optionId -> {loading, ok, message, options}
    sourceFilter: "",
    scanMedia: "",
    // users page
    users: [],
    userMsg: {},
    creds: { username: "", password: "" },
    setup: { step: 0, code: "", url: "", adminRole: "Admin", username: "", password: "", test: null },
    adminNav: ADMIN_NAV,
    // discover / search / details
    media: "",                 // current media tab for Discover/Search
    rows: {},                  // media -> {loading, error, rows}
    q: "",
    results: [], searching: false, searchError: "", searched: "",
    detail: null,              // item shown in the modal
    detailBusy: false, detailMsg: null,
    quota: null,
    // requests
    reqs: { scope: "mine", status: "active", media: "", q: "", list: [], total: 0, counts: {}, loading: false, error: "" },
    reqMsg: {},
    pollTimer: null,
    // choose-source modal
    picker: null,
    // discover / jobs / logs admin
    discoverStatus: null,
    jobs: null,
    logs: { level: "info", q: "", entries: [], loading: false },
    about: null, update: null, audit: [], importInfo: null, importMsg: null,
    statusLabel: STATUS_LABEL, statusTone: STATUS_TONE, requestFilters: REQUEST_FILTERS,

    get visibleTabs() { return TABS.filter((t) => !t.admin || this.me?.is_admin); },
    get enabledMediaTypes() { return MEDIA.filter((m) => this.site.media_types?.[m.id]); },

    init() {
      window.addEventListener("hashchange", () => this.route());
      this.boot();
    },

    async boot() {
      this.view = "boot";
      this.error = "";
      try {
        this.site = await api("GET", "api/system/info");
        document.title = this.site.site_name;
        document.documentElement.dataset.theme = this.site.theme || "dark";
        if (!this.site.setup_completed) return this.startSetup();
        try {
          this.setMe(await api("GET", "api/auth/me"));
          this.view = "app";
          this.route();
        } catch (e) {
          if (e.status !== 401) throw e;
          this.showLogin();
        }
      } catch (e) {
        this.fatal = e.message;
        this.view = "error";
      }
    },

    setMe(me) { this.me = me; csrfToken = me?.csrf_token || ""; },

    showLogin() {
      this.view = "login";
      this.$nextTick(() => this.$refs.loginUser?.focus());
    },

    route() {
      const parts = location.hash.replace(/^#\/?/, "").split("/");
      const id = parts[0] || this.site.default_tab || "discover";
      const known = this.visibleTabs.some((t) => t.id === id);
      this.tab = known ? id : "discover";
      if (!this.media || !this.site.media_types?.[this.media]) this.media = this.enabledMediaTypes[0]?.id || "";
      if (this.tab === "discover") this.loadRows(this.media);
      if (this.tab === "requests") { this.reqs.scope = parts[1] === "all" && this.me?.is_admin ? "all" : "mine"; this.loadRequests(); }
      if (this.tab === "search") this.$nextTick(() => this.$refs.searchBox?.focus());
      if (this.tab === "admin") {
        const page = ADMIN_NAV.find((p) => p.id === parts[1]) ? parts[1] : "overview";
        this.openAdminPage(page);
      }
      this.schedulePoll();
    },

    goAdmin(id) { location.hash = id === "overview" ? "#/admin" : `#/admin/${id}`; },

    async openAdminPage(id) {
      this.adminPage = id;
      this.pageMsg = null;
      try {
        if (id === "overview") this.admin = await api("GET", "api/admin/overview");
        else if (id === "users") await this.loadUsers();
        else if (id === "logs") await this.loadLogsPage();
        else if (this.navItem(id)?.schema) {
          await this.ensureSettings();
          this.autoLoadOptions(this.page);
          if (id === "discover") this.loadDiscoverStatus();
          if (id === "jobs") this.loadJobs();
        }
      } catch (e) { if (e.status === 401) this.showLogin(); else this.pageMsg = { ok: false, message: e.message }; }
    },

    navItem(id) { return ADMIN_NAV.find((p) => p.id === id); },
    get page() { return this.schema.find((p) => p.id === this.adminPage); },

    // ---------------------------------------------------------------- settings
    async ensureSettings() {
      if (!this.schema.length) this.schema = (await api("GET", "api/admin/settings/schema")).pages;
      if (!Object.keys(this.values).length) this.applyValues((await api("GET", "api/admin/settings")).values, true);
    },

    applyValues(values, resetDrafts) {
      this.values = values;
      for (const p of this.schema) for (const f of p.fields) {
        if (f.widget === "secret") {
          if (resetDrafts || !this.secretDirty(f.key)) { this.secretDraft[f.key] = ""; this.secretClear[f.key] = false; }
        } else if (resetDrafts || !this.dirtyKey(f)) {
          const v = values[f.key];
          this.draft[f.key] = Array.isArray(v) ? [...v] : v;
        }
      }
    },

    fieldsOf(pageId) { return this.schema.find((p) => p.id === pageId)?.fields || []; },

    visible(f) {
      if (!f.show_if) return true;
      return f.show_if.values.includes(String(this.draft[f.show_if.key]));
    },

    secretDirty(key) { return !!this.secretDraft[key] || !!this.secretClear[key]; },

    dirtyKey(f) {
      if (f.widget === "secret") return this.secretDirty(f.key);
      return !same(normalise(f, this.draft[f.key]), normalise(f, this.values[f.key]));
    },

    pageDirty(pageId) { return this.fieldsOf(pageId).some((f) => this.dirtyKey(f)); },

    // Values to send: for saving (changed keys only) or for test/options (all page keys).
    collect(pageId, onlyDirty) {
      const out = {};
      for (const f of this.fieldsOf(pageId)) {
        if (onlyDirty && !this.dirtyKey(f)) continue;
        if (f.widget === "secret") {
          out[f.key] = this.secretClear[f.key] ? "" : (this.secretDraft[f.key] || null);
        } else {
          out[f.key] = normalise(f, this.draft[f.key]);
        }
      }
      return out;
    },

    async savePage() {
      const p = this.page; if (!p) return;
      this.busy = true; this.fieldErrors = {}; this.pageMsg = null;
      try {
        const r = await api("PUT", "api/admin/settings", { values: this.collect(p.id, true) });
        for (const f of p.fields) {
          if (f.widget === "secret") { this.secretDraft[f.key] = ""; this.secretClear[f.key] = false; }
        }
        this.applyValues(r.values, false);
        for (const f of p.fields) {  // re-sync this page's drafts to what the server stored
          if (f.widget !== "secret") { const v = r.values[f.key]; this.draft[f.key] = Array.isArray(v) ? [...v] : v; }
        }
        this.pageMsg = { ok: true, message: r.changed.length ? "Saved. Changes apply immediately." : "Nothing to save." };
        if (p.id === "general") await this.refreshSite();
      } catch (e) {
        if (e.status === 422 && e.detail?.key) {
          this.fieldErrors = { [e.detail.key]: e.detail.message };
          this.pageMsg = { ok: false, message: "Not saved — fix the highlighted field." };
        } else this.pageMsg = { ok: false, message: e.message };
      } finally { this.busy = false; }
    },

    discardPage() {
      const p = this.page; if (!p) return;
      for (const f of p.fields) {
        if (f.widget === "secret") { this.secretDraft[f.key] = ""; this.secretClear[f.key] = false; }
        else { const v = this.values[f.key]; this.draft[f.key] = Array.isArray(v) ? [...v] : v; }
      }
      this.fieldErrors = {}; this.pageMsg = null;
    },

    async testPage() {
      const p = this.page; if (!p?.connection) return;
      this.testResult[p.id] = { loading: true, message: "Testing…" };
      try {
        const r = await api("POST", `api/admin/connections/${p.connection}/test`, { values: this.collect(p.id, false) });
        this.testResult[p.id] = r;
        if (r.ok) this.autoLoadOptions(p, true);
      } catch (e) {
        this.testResult[p.id] = { ok: false, message: e.detail?.message || e.message };
      }
    },

    autoLoadOptions(p, force) {
      if (!p) return;
      const urlKey = SERVICE_URL_KEY[p.connection];
      if (urlKey && !normalise({ type: "url" }, this.draft[urlKey])) return;
      for (const f of p.fields) if (f.options && (force || !this.opts[f.options])) this.loadOptions(f.options, p.id);
    },

    async loadOptions(optionId, pageId) {
      this.opts[optionId] = { loading: true, ok: true, message: "", options: this.opts[optionId]?.options || [] };
      try {
        const r = await api("POST", `api/admin/options/${optionId}`, { values: this.collect(pageId || this.adminPage, false) });
        this.opts[optionId] = { loading: false, ...r };
      } catch (e) {
        this.opts[optionId] = { loading: false, ok: false, message: e.detail?.message || e.message, options: [] };
      }
    },

    optionsFor(f) { return this.opts[f.options]?.options || []; },
    optionLabel(f, value) {
      const o = this.optionsFor(f).find((x) => x.value === value);
      return o ? o.label : value;
    },
    unknownSelected(f) {
      const v = this.draft[f.key];
      return v && !this.opts[f.options]?.loading && !this.optionsFor(f).some((o) => o.value === v);
    },

    // source-list widget
    selectedSources(f) { return this.draft[f.key] || []; },
    availableSources(f) {
      const langs = f.key === "suwayomi.sources" ? normalise({ type: "list" }, this.draft["suwayomi.source_languages"]) : [];
      const q = this.sourceFilter.trim().toLowerCase();
      const chosen = new Set(this.selectedSources(f));
      return this.optionsFor(f).filter((o) =>
        !chosen.has(o.value) &&
        (!langs.length || langs.includes(o.hint)) &&
        (!q || o.label.toLowerCase().includes(q)));
    },
    addSource(f, id) {
      id = String(id || "").trim();
      if (id && !this.selectedSources(f).includes(id)) this.draft[f.key] = [...this.selectedSources(f), id];
    },
    removeSource(f, id) { this.draft[f.key] = this.selectedSources(f).filter((x) => x !== id); },
    moveSource(f, i, delta) {
      const list = [...this.selectedSources(f)]; const j = i + delta;
      if (j < 0 || j >= list.length) return;
      [list[i], list[j]] = [list[j], list[i]];
      this.draft[f.key] = list;
    },

    async scanKavita() {
      this.busy = true; this.pageMsg = null;
      try {
        const r = await api("POST", "api/admin/kavita/scan", this.scanMedia ? { media_type: this.scanMedia } : {});
        this.pageMsg = { ok: true, message: r.message };
      } catch (e) { this.pageMsg = { ok: false, message: e.message }; }
      finally { this.busy = false; }
    },

    async uploadLogo(event) {
      const file = event.target.files?.[0]; event.target.value = "";
      if (!file) return;
      this.busy = true; this.pageMsg = null;
      try {
        const res = await fetch("api/admin/branding/logo", {
          method: "PUT", body: file, credentials: "same-origin",
          headers: { "Content-Type": file.type || "application/octet-stream", "X-CSRF-Token": csrfToken },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `Upload failed (HTTP ${res.status})`);
        await this.refreshSite();
        this.pageMsg = { ok: true, message: "Logo updated." };
      } catch (e) { this.pageMsg = { ok: false, message: e.message }; }
      finally { this.busy = false; }
    },

    async removeLogo() {
      try { await api("DELETE", "api/admin/branding/logo"); await this.refreshSite(); this.pageMsg = { ok: true, message: "Logo removed." }; }
      catch (e) { this.pageMsg = { ok: false, message: e.message }; }
    },

    async refreshSite() {
      this.site = await api("GET", "api/system/info");
      document.title = this.site.site_name;
      document.documentElement.dataset.theme = this.site.theme || "dark";
    },

    get logoUrl() { return this.site.logo_version ? `api/branding/logo?v=${this.site.logo_version}` : ""; },


    // ---------------------------------------------------------------- helpers
    mediaLabel(id) { return MEDIA.find((m) => m.id === id)?.label || id; },
    unitFor(media) { return media === "comic" ? "issues" : "chapters"; },
    ago(iso) {
      if (!iso) return "never";
      const s = (Date.now() - new Date(iso).getTime()) / 1000;
      if (s < 0) { const f = -s; return f < 90 ? "in a minute" : f < 5400 ? `in ${Math.round(f / 60)} min` : `in ${Math.round(f / 3600)} h`; }
      if (s < 60) return "just now";
      if (s < 3600) return `${Math.round(s / 60)} min ago`;
      if (s < 86400) return `${Math.round(s / 3600)} h ago`;
      return `${Math.round(s / 86400)} d ago`;
    },
    badgeFor(item) {
      if (item.request) return { label: STATUS_LABEL[item.request.status] || item.request.status, tone: STATUS_TONE[item.request.status] || "" };
      if (item.in_library) return { label: "In library", tone: "ok" };
      return null;
    },
    fmtBytes(n) { if (!n) return "0 B"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; },
    fmtDuration(s) { if (s == null) return ""; if (s < 120) return `${s}s`; if (s < 7200) return `${Math.round(s / 60)} min`; if (s < 172800) return `${Math.round(s / 3600)} h`; return `${Math.round(s / 86400)} days`; },

    // ---------------------------------------------------------------- discover
    setMedia(id) {
      this.media = id;
      if (this.tab === "discover") this.loadRows(id);
      if (this.tab === "search" && this.q.trim()) this.runSearch();
    },
    async loadRows(media, force) {
      if (!media) return;
      if (this.rows[media] && !force && Date.now() - this.rows[media].at < 60000) return;
      this.rows[media] = { ...(this.rows[media] || { rows: [] }), loading: true, error: "" };
      try {
        const r = await api("GET", `api/discover?media_type=${media}`);
        this.rows[media] = { loading: false, error: "", rows: r.rows, at: Date.now() };
      } catch (e) {
        if (e.status === 401) return this.showLogin();
        this.rows[media] = { loading: false, error: e.message, rows: [], at: 0 };
      }
    },
    get currentRows() { return this.rows[this.media]?.rows || []; },
    get visibleRows() { return this.currentRows.filter((r) => r.items.length || r.pending); },
    async refreshDiscoverNow(media) {
      try {
        const r = await api("POST", "api/admin/discover/refresh", { media: media === "comic" ? "comic" : (media ? "manga" : "all") });
        this.pageMsg = { ok: true, message: r.message };
        this.flash(r.message);
      } catch (e) { this.pageMsg = { ok: false, message: e.message }; this.flash(e.message, false); }
    },
    toast: null,
    flash(message, ok = true) { this.toast = { message, ok }; clearTimeout(this._toastT); this._toastT = setTimeout(() => (this.toast = null), 4000); },

    // ---------------------------------------------------------------- search
    async runSearch() {
      const q = this.q.trim();
      if (!q || !this.media) return;
      this.searching = true; this.searchError = ""; this.searched = q;
      try {
        this.results = (await api("GET", `api/search?media_type=${this.media}&q=${encodeURIComponent(q)}`)).results;
      } catch (e) {
        if (e.status === 401) return this.showLogin();
        this.results = []; this.searchError = e.message;
      } finally { this.searching = false; }
    },

    // ---------------------------------------------------------------- details + request
    async openItem(item) {
      this.detail = { ...item }; this.detailMsg = null;
      this.loadQuota();
      if (item.provider === "comicvine" && item.description) return;
      try {
        const full = await api("GET", `api/titles/${item.media_type}/${item.provider}/${item.external_id}`);
        if (this.detail?.key === item.key) this.detail = { ...item, ...full };
      } catch { /* the card's data is enough to request */ }
    },
    closeDetail() { this.detail = null; },
    async loadQuota() { try { this.quota = await api("GET", "api/requests/quota"); } catch { this.quota = null; } },
    async requestItem(item) {
      this.detailBusy = true; this.detailMsg = null;
      try {
        const r = await api("POST", "api/requests", {
          media_type: item.media_type, provider: item.provider, external_id: item.external_id,
          hints: { title: item.title, year: item.year, cover_url: item.cover_url, publisher: item.publisher,
                   issue_count: item.issue_count, alt_titles: item.alt_titles, anilist_id: item.anilist_id },
        });
        const state = { id: r.id, status: r.status, mine: true };
        this.markRequested(item.key, state);
        if (this.detail) this.detail.request = state;
        this.detailMsg = { ok: true, message: r.status === "pending" ? "Requested — waiting for an admin to approve." : "Requested! It's on its way." };
        this.loadQuota();
      } catch (e) {
        if (e.status === 409 && e.detail?.status) {
          const state = { id: e.detail.request_id, status: e.detail.status, mine: false };
          this.markRequested(item.key, state); if (this.detail) this.detail.request = state;
        }
        this.detailMsg = { ok: false, message: e.message };
      } finally { this.detailBusy = false; }
    },
    markRequested(key, state) {
      for (const r of this.results) if (r.key === key) r.request = state;
      for (const m of Object.values(this.rows)) for (const row of m.rows || []) for (const i of row.items) if (i.key === key) i.request = state;
    },

    // ---------------------------------------------------------------- requests
    async loadRequests() {
      const r = this.reqs; r.loading = true; r.error = "";
      const params = new URLSearchParams({ scope: r.scope, limit: "200" });
      if (r.status) params.set("status", r.status);
      if (r.media) params.set("media_type", r.media);
      if (r.q.trim()) params.set("q", r.q.trim());
      try {
        const data = await api("GET", `api/requests?${params}`);
        r.list = data.requests; r.total = data.total; r.counts = data.counts;
      } catch (e) {
        if (e.status === 401) return this.showLogin();
        r.error = e.message;
      } finally { r.loading = false; this.schedulePoll(); }
    },
    setReqScope(scope) { this.reqs.scope = scope; this.loadRequests(); },
    setReqStatus(s) { this.reqs.status = s; this.loadRequests(); },
    countFor(id) {
      const c = this.reqs.counts || {};
      if (id === "") return Object.values(c).reduce((a, b) => a + b, 0);
      if (id === "active") return Object.entries(c).filter(([k]) => !["in_library", "failed", "denied"].includes(k)).reduce((a, [, n]) => a + n, 0);
      return c[id] || 0;
    },
    schedulePoll() {
      clearTimeout(this.pollTimer);
      const watching = this.tab === "requests" && this.reqs.list.some((r) => BUSY_STATUSES.has(r.status));
      const jobsRunning = this.tab === "admin" && this.adminPage === "jobs" && this.jobs?.jobs?.some((j) => j.running);
      if (watching || jobsRunning) this.pollTimer = setTimeout(() => (jobsRunning ? this.loadJobs() : this.loadRequests()), 2500);
    },
    async reqAction(r, action, body) {
      this.reqMsg[r.id] = { busy: true };
      try {
        let updated;
        if (action === "delete") { await api("DELETE", `api/requests/${r.id}`); this.reqs.list = this.reqs.list.filter((x) => x.id !== r.id); this.reqMsg[r.id] = null; this.loadRequests(); return; }
        updated = await api("POST", `api/requests/${r.id}/${action}`, body);
        Object.assign(r, updated);
        this.reqMsg[r.id] = { ok: true, message: { approve: "Approved", deny: "Denied", retry: "Retrying…", refresh: "Refreshed" }[action] || "Done" };
        this.schedulePoll();
      } catch (e) { this.reqMsg[r.id] = { ok: false, message: e.message }; }
    },
    confirmDelete(r) {
      const what = r.downloader_ref ? " It stays in the downloader — remove it there too if you want it gone." : "";
      if (confirm(`Remove the request for “${r.title}”?${what}`)) this.reqAction(r, "delete");
    },
    denyPrompt(r) {
      const reason = prompt(`Deny “${r.title}”? Optional reason shown to the requester:`, "");
      if (reason !== null) this.reqAction(r, "deny", { reason });
    },
    canCancel(r) { return r.mine && ["pending", "failed", "denied", "waiting_for_source", "waiting_for_downloader"].includes(r.status); },

    // choose source (manga)
    async openPicker(r, q) {
      this.picker = { req: r, q: q ?? "", loading: true, error: "", candidates: [], errors: [], sources: [], source: this.picker?.source || "" };
      await this.searchPicker();
    },
    async searchPicker() {
      const p = this.picker; if (!p) return;
      p.loading = true; p.error = "";
      const params = new URLSearchParams();
      if (p.q.trim()) params.set("q", p.q.trim());
      if (p.source) params.set("source_id", p.source);
      try {
        const r = await api("GET", `api/requests/${p.req.id}/candidates?${params}`);
        Object.assign(p, { candidates: r.candidates, errors: r.errors, sources: r.sources, titles: r.titles });
        if (!p.q) p.q = "";
      } catch (e) { p.error = e.message; p.candidates = []; }
      finally { p.loading = false; }
    },
    async pickCandidate(c) {
      const p = this.picker; if (!p) return;
      p.loading = true;
      try {
        const updated = await api("POST", `api/requests/${p.req.id}/source`, { source_id: c.source_id, manga_id: c.manga_id, title: c.title });
        Object.assign(p.req, updated);
        this.picker = null;
        this.flash(`Sent to ${updated.source_name}.`);
      } catch (e) { p.error = e.message; p.loading = false; }
    },

    // ---------------------------------------------------------------- admin: discover / jobs / logs
    async loadDiscoverStatus() { try { this.discoverStatus = await api("GET", "api/admin/discover/status"); } catch (e) { this.pageMsg = { ok: false, message: e.message }; } },
    async rematch() {
      try { const r = await api("POST", "api/admin/discover/rematch"); this.pageMsg = { ok: true, message: r.message }; this.loadDiscoverStatus(); }
      catch (e) { this.pageMsg = { ok: false, message: e.message }; }
    },
    async loadJobs() {
      try { this.jobs = await api("GET", "api/admin/jobs"); } catch (e) { this.pageMsg = { ok: false, message: e.message }; }
      this.schedulePoll();
    },
    async runJob(j) {
      try { await api("POST", `api/admin/jobs/${j.id}/run`); j.running = true; this.flash(`${j.label} started.`); setTimeout(() => this.loadJobs(), 800); }
      catch (e) { this.flash(e.message, false); }
    },
    async loadLogsPage() {
      const [about, audit, importInfo] = await Promise.all([
        api("GET", "api/admin/about"), api("GET", "api/admin/audit?limit=100"), api("GET", "api/admin/import/v1"),
      ]);
      this.about = about; this.audit = audit.entries; this.importInfo = importInfo;
      this.loadLogs();
    },
    async loadLogs() {
      this.logs.loading = true;
      try {
        const p = new URLSearchParams({ level: this.logs.level, lines: "400" });
        if (this.logs.q.trim()) p.set("q", this.logs.q.trim());
        this.logs.entries = (await api("GET", `api/admin/logs?${p}`)).entries.reverse();
      } catch (e) { this.pageMsg = { ok: false, message: e.message }; }
      finally { this.logs.loading = false; }
    },
    logExtras(e) {
      const skip = new Set(["ts", "level", "logger", "msg", "exc"]);
      return Object.entries(e).filter(([k]) => !skip.has(k)).map(([k, v]) => `${k}=${v}`).join("  ");
    },
    async checkUpdate() { this.update = { loading: true }; try { this.update = await api("GET", "api/admin/update-check"); } catch (e) { this.update = { ok: false, message: e.message }; } },
    async rawUpload(method, path, file, contentType) {
      const res = await fetch(path, { method, body: file, credentials: "same-origin",
        headers: { "Content-Type": contentType || "application/octet-stream", "X-CSRF-Token": csrfToken } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { const d = data.detail; throw new Error((d && typeof d === "object" ? d.message : d) || `HTTP ${res.status}`); }
      return data;
    },
    async importSettingsFile(event) {
      const file = event.target.files?.[0]; event.target.value = ""; if (!file) return;
      try {
        const r = await this.rawUpload("POST", "api/admin/settings/import", file, "application/json");
        this.importMsg = { ok: true, message: r.message };
        this.values = {}; await this.ensureSettings();
        await this.refreshSite();
      } catch (e) { this.importMsg = { ok: false, message: e.message }; }
    },
    async importV1(mode, event) {
      this.importMsg = { ok: true, message: "Importing…" };
      try {
        let r;
        if (mode === "upload") {
          const file = event.target.files?.[0]; event.target.value = ""; if (!file) { this.importMsg = null; return; }
          r = await this.rawUpload("PUT", "api/admin/import/v1/upload", file);
        } else r = await api("POST", "api/admin/import/v1/mounted");
        this.importMsg = { ok: true, message: r.summary + (r.warnings.length ? ` ${r.warnings.length} warning(s).` : "") };
        this.importInfo = await api("GET", "api/admin/import/v1");
      } catch (e) { this.importMsg = { ok: false, message: e.message }; }
    },

    // ---------------------------------------------------------------- users
    async loadUsers() { this.users = await api("GET", "api/admin/users"); },

    async patchUser(u, changes) {
      this.userMsg[u.id] = { saving: true };
      try {
        const updated = await api("PATCH", `api/admin/users/${u.id}`, changes);
        Object.assign(u, updated);
        this.userMsg[u.id] = { ok: true, message: "Saved" };
        setTimeout(() => { if (this.userMsg[u.id]?.ok) this.userMsg[u.id] = null; }, 1800);
      } catch (e) {
        this.userMsg[u.id] = { ok: false, message: e.message };
        await this.loadUsers();
      }
    },

    toggleUserMedia(u, media, on) {
      const all = MEDIA.map((m) => m.id);
      let cur = u.allowed_media_types ?? [...all];
      cur = on ? [...new Set([...cur, media])] : cur.filter((m) => m !== media);
      this.patchUser(u, { allowed_media_types: cur.length === all.length ? null : cur });
    },

    userLimitChanged(u, raw) {
      const v = String(raw).trim();
      this.patchUser(u, { daily_limit_override: v === "" ? null : Math.max(0, parseInt(v, 10) || 0) });
    },

    fmtDate(iso) {
      if (!iso) return "—";
      const d = new Date(iso);
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) + " " +
        d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    },

    async login() {
      this.busy = true; this.error = "";
      try {
        this.setMe(await api("POST", "api/auth/login", this.creds));
        this.creds.password = "";
        this.view = "app";
        this.route();
      } catch (e) { this.error = e.message; }
      finally { this.busy = false; }
    },

    async logout() {
      try { await api("POST", "api/auth/logout"); } catch { /* session may already be gone */ }
      this.setMe(null);
      this.admin = null;
      this.schema = []; this.values = {}; this.draft = {}; this.secretDraft = {}; this.secretClear = {};
      this.opts = {}; this.users = []; this.testResult = {};
      this.rows = {}; this.results = []; this.detail = null; this.picker = null; this.quota = null;
      this.reqs.list = []; this.reqs.scope = "mine";
      history.replaceState(null, "", location.pathname);
      this.showLogin();
    },

    // ---- first-run wizard ----
    async startSetup() {
      const s = await api("GET", "api/setup/status");
      this.setup.adminRole = s.admin_role;
      this.setup.step = 0;
      this.view = "setup";
      this.$nextTick(() => this.$refs.setupCode?.focus());
    },

    async setupClaim() {
      this.busy = true; this.error = "";
      try {
        const r = await api("POST", "api/setup/claim", { code: this.setup.code });
        if (r.kavita_url && !this.setup.url) this.setup.url = r.kavita_url;
        this.setup.step = 1;
      } catch (e) {
        if (e.status === 409) return this.boot();
        this.error = e.message;
      } finally { this.busy = false; }
    },

    async setupTest() {
      this.busy = true; this.setup.test = null;
      try {
        this.setup.test = await api("POST", "api/setup/kavita/test",
          { code: this.setup.code, url: this.setup.url });
      } catch (e) {
        if (e.status === 403 && /setup code/i.test(e.message)) { this.setup.step = 0; this.error = e.message; }
        this.setup.test = { ok: false, message: e.message };
      } finally { this.busy = false; }
      return this.setup.test.ok;
    },

    async setupNext() {
      if (await this.setupTest()) { this.setup.step = 2; this.error = ""; }
    },

    async setupComplete() {
      this.busy = true; this.error = "";
      try {
        const me = await api("POST", "api/setup/complete", {
          code: this.setup.code, kavita_url: this.setup.url,
          username: this.setup.username, password: this.setup.password,
        });
        this.setup.password = "";
        this.setup.code = "";
        this.site.setup_completed = true;
        this.setMe(me);
        this.view = "app";
        location.hash = "#/admin";
        this.route();
      } catch (e) {
        if (e.status === 409) return this.boot();
        this.error = e.message;
      } finally { this.busy = false; }
    },
  }));
});
