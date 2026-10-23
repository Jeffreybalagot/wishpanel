/* WishPanel UI shell (Alpine.js). All URLs are relative so BASE_URL prefixes work. */
"use strict";

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
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
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new ApiError(res.status, msg || `Request failed (HTTP ${res.status})`);
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

// Mirrors SPEC §7 — each becomes a real page in Milestone 2.
const ADMIN_PAGES = [
  ["overview", "Overview"], ["general", "General"], ["authentication", "Authentication"],
  ["users", "Users"], ["media-types", "Media Types"], ["kavita", "Library — Kavita"],
  ["kapowarr", "Downloader — Kapowarr"], ["suwayomi", "Downloader — Suwayomi"],
  ["comicvine", "Metadata — ComicVine"], ["anilist", "Metadata — AniList / MangaBaka"],
  ["discover", "Discover"], ["requests", "Requests"], ["jobs", "Jobs"], ["logs", "Logs & About"],
].map(([id, label]) => ({ id, label }));

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
    creds: { username: "", password: "" },
    setup: { step: 1, url: "", adminRole: "Admin", username: "", password: "", test: null },
    adminPages: ADMIN_PAGES,

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
      const id = (location.hash.replace(/^#\/?/, "").split("/")[0]) || this.site.default_tab || "discover";
      const known = this.visibleTabs.some((t) => t.id === id);
      this.tab = known ? id : "discover";
      if (this.tab === "admin") this.loadAdmin();
    },

    async loadAdmin() {
      try { this.admin = await api("GET", "api/admin/overview"); }
      catch (e) { if (e.status === 401) this.showLogin(); }
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
      history.replaceState(null, "", location.pathname);
      this.showLogin();
    },

    // ---- first-run wizard ----
    async startSetup() {
      const s = await api("GET", "api/setup/status");
      this.setup.url = s.kavita_url || "";
      this.setup.adminRole = s.admin_role;
      this.view = "setup";
    },

    async setupTest() {
      this.busy = true; this.setup.test = null;
      try { this.setup.test = await api("POST", "api/setup/kavita/test", { url: this.setup.url }); }
      catch (e) { this.setup.test = { ok: false, message: e.message }; }
      finally { this.busy = false; }
      return this.setup.test.ok;
    },

    async setupNext() {
      if (await this.setupTest()) { this.setup.step = 2; this.error = ""; }
    },

    async setupComplete() {
      this.busy = true; this.error = "";
      try {
        const me = await api("POST", "api/setup/complete", {
          kavita_url: this.setup.url, username: this.setup.username, password: this.setup.password,
        });
        this.setup.password = "";
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
