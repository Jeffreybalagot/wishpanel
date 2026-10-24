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
  { id: "media-types", label: "Media Types", milestone: 3 },
  { id: "kavita", label: "Library — Kavita", schema: true },
  { id: "kapowarr", label: "Downloader — Kapowarr", schema: true },
  { id: "suwayomi", label: "Downloader — Suwayomi", schema: true },
  { id: "comicvine", label: "Metadata — ComicVine", schema: true },
  { id: "anilist", label: "Metadata — AniList / MangaBaka", milestone: 4 },
  { id: "discover", label: "Discover", milestone: 4 },
  { id: "requests", label: "Requests", milestone: 3 },
  { id: "jobs", label: "Jobs", milestone: 4 },
  { id: "logs", label: "Logs & About", milestone: 5 },
];

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
      if (this.tab === "admin") {
        const page = ADMIN_NAV.find((p) => p.id === parts[1]) ? parts[1] : "overview";
        this.openAdminPage(page);
      }
    },

    goAdmin(id) { location.hash = id === "overview" ? "#/admin" : `#/admin/${id}`; },

    async openAdminPage(id) {
      this.adminPage = id;
      this.pageMsg = null;
      try {
        if (id === "overview") this.admin = await api("GET", "api/admin/overview");
        else if (id === "users") await this.loadUsers();
        else if (this.navItem(id)?.schema) {
          await this.ensureSettings();
          this.autoLoadOptions(this.page);
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
      return f.show_if.values.includes(this.draft[f.show_if.key]);
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
      const langs = normalise({ type: "list" }, this.draft["suwayomi.source_languages"]);
      const q = this.sourceFilter.trim().toLowerCase();
      const chosen = new Set(this.selectedSources(f));
      return this.optionsFor(f).filter((o) =>
        !chosen.has(o.value) &&
        (!langs.length || langs.includes(o.hint)) &&
        (!q || o.label.toLowerCase().includes(q)));
    },
    addSource(f, id) { this.draft[f.key] = [...this.selectedSources(f), id]; },
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
