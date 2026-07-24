import React, { useState, useEffect, useMemo } from "react";

// ─────────────────────────────────────────────────────────────
//  ВікіРанг / WikiRank — аналіз внеску учасників вікіконкурсів
//  Ролі: адмін (налаштування) та журі (оцінювання)
//  Мови інтерфейсу: українська / English
// ─────────────────────────────────────────────────────────────

const K_USERS = "wr_users";
const K_CONTESTS = "wr_contests";
const kAssess = (id) => `wr_assess_${id}`;
const kResults = (id) => `wr_results_${id}`;

// ── Переклади ────────────────────────────────────────────────
const T = {
  uk: {
    brand: "ВікіРанг",
    tagline: "Аналіз внеску учасників вікіпроєктів. Доступ лише для адмінів та журі.",
    login: "Логін", password: "Пароль", signin: "Увійти",
    badCreds: "Невірний логін або пароль",
    firstRun: "Перший запуск: створено адміна {c}. Змініть пароль у налаштуваннях.",
    admin: "адмін", jury: "журі", youAre: "це ви", logout: "Вийти",
    navAnalysis: "Оцінювання", navSettings: "Налаштування",
    tabContests: "Конкурси", tabUsers: "Журі та адміни", tabPassword: "Мій пароль",
    newContest: "Новий конкурс", editContest: "Редагувати конкурс",
    contestName: "Назва конкурсу",
    projectsLabel: "Проєкти — по одному посиланню в рядку",
    projectsHint: "Будь-який мовний розділ MediaWiki: uk.wikivoyage.org, en.wikivoyage.org, en.wikiquote.org тощо — просто вставте посилання.",
    dateStart: "Початок періоду (необов'язково)", dateEnd: "Кінець періоду",
    tplLabel: "Шаблон конкурсу — ідентифікує статті кампанії",
    tplExtract: "Витягнути учасників із шаблону", tplExtracting: "Витягую…",
    tplHint: "Інструмент знайде всі статті, що містять шаблон, і збере авторів редагувань у межах вказаних дат (боти відсіюються). Знайдені імена додаються до списку нижче — його можна відредагувати вручну.",
    tplBad: "Вставте коректне посилання на шаблон конкурсу",
    tplNone: "Жодна стаття не містить цього шаблону",
    tplSearch: "Шукаю сторінки з шаблоном…",
    tplPage: "Сторінка", tplDone: "Знайдено статей: {p}, учасників: {u} ✓",
    tplErr: "Помилка витягування: ",
    participantsLabel: "Учасники кампанії — по одному імені користувача в рядку",
    participantsPh: "Ім'я користувача 1\nІм'я користувача 2\n(або скористайтеся витягуванням із шаблону вище)",
    formulaLabel: "Формула ранжування для журі",
    formulaHint: "Змінні:", vBytes: "додані байти", vEdits: "редагування", vArticles: "сторінки", vQuality: "середня оцінка журі",
    saveChanges: "Зберегти зміни", createContest: "Створити конкурс", cancel: "Скасувати",
    contests: "Конкурси", noContests: "Ще немає жодного конкурсу — створіть перший ліворуч.",
    participantsN: "учасників", tpl: "шаблон", edit: "Редагувати", del: "Видалити",
    saved: "Збережено ✓",
    errName: "Вкажіть назву конкурсу", errProjects: "Додайте хоча б одне посилання на проєкт", errUrl: "Некоректне посилання: ",
    fEmpty: "Формула порожня",
    fChars: "Дозволені лише числа, дужки, + − * / та змінні: ",
    fNaN: "Формула не повертає число", fSyntax: "Синтаксична помилка у формулі",
    addUser: "Додати обліковий запис", role: "Роль",
    nameVisible: "Ім'я (видиме в коментарях)",
    userCreated: "Обліковий запис створено ✓",
    errUserReq: "Логін і пароль обов'язкові", errUserDup: "Такий логін уже існує",
    accounts: "Облікові записи", create: "Створити",
    chPass: "Зміна пароля", newPass: "Новий пароль", again: "Ще раз",
    passShort: "Пароль закороткий (мінімум 4 символи)", passDiff: "Паролі не збігаються", passOk: "Пароль змінено ✓", save: "Зберегти",
    mode: "Режим аналізу", mQuant: "Кількісний", mQual: "Якісний",
    qualAuto: "Якісний аналіз автоматично показує й кількісний внесок.",
    modesHint: "Режими можна вмикати окремо або разом.",
    check: "Перевірити внесок", checking: "Перевіряю…", exportCsv: "Експорт CSV ↓",
    thUser: "Учасник", thBytes: "Додано байтів", thEdits: "Ред. / стор.",
    thMy: "Моя оцінка", thAvg: "Сер. якість", thScore: "Бал за формулою",
    scores: "оцін.", comments: "Коментарі", noComments: "Коментарів ще немає.",
    commentPh: "Ваш коментар до внеску учасника…",
    error: "помилка",
    noParticipants: "У цьому конкурсі ще немає учасників — адмін може додати їх у налаштуваннях.",
    formulaOf: "Формула конкурсу:", formulaNote: "бал перераховується автоматично після перевірки внеску та виставлення оцінок журі.",
    noContest: "Немає жодного конкурсу.", createIn: "Створіть його у «Налаштуваннях».", askAdmin: "Зверніться до адміністратора.",
    footer: "Дані конкурсів, оцінки та коментарі зберігаються у спільному сховищі й видимі всім користувачам цього інструмента. Кількісний внесок рахується через API MediaWiki як сума доданих байтів (позитивні зміни розміру).",
    loading: "Завантаження…",
    scoreFor: "Оцінка для ",
    csvN: "№", csvUser: "Учасник", csvBytesTotal: "Додано байтів (усього)", csvBytes: "Байти: ",
    csvEdits: "Редагувань", csvArticles: "Сторінок", csvAvg: "Середня якість", csvCnt: "К-сть оцінок",
    csvScore: "Бал за формулою", csvComments: "Коментарі",
  },
  en: {
    brand: "WikiRank",
    tagline: "Contribution analysis for wiki contests. Access for admins and jury only.",
    login: "Login", password: "Password", signin: "Sign in",
    badCreds: "Wrong login or password",
    firstRun: "First run: admin account {c} created. Change the password in settings.",
    admin: "admin", jury: "jury", youAre: "that's you", logout: "Log out",
    navAnalysis: "Assessment", navSettings: "Settings",
    tabContests: "Contests", tabUsers: "Jury & admins", tabPassword: "My password",
    newContest: "New contest", editContest: "Edit contest",
    contestName: "Contest name",
    projectsLabel: "Projects — one link per line",
    projectsHint: "Any MediaWiki language edition: uk.wikivoyage.org, en.wikivoyage.org, en.wikiquote.org etc. — just paste a link.",
    dateStart: "Period start (optional)", dateEnd: "Period end",
    tplLabel: "Contest template — identifies campaign articles",
    tplExtract: "Extract participants from template", tplExtracting: "Extracting…",
    tplHint: "The tool finds every article that transcludes the template and collects revision authors within the given dates (bots are filtered out). Found names are merged into the list below — you can edit it manually.",
    tplBad: "Paste a valid link to the contest template",
    tplNone: "No article transcludes this template",
    tplSearch: "Searching pages with the template…",
    tplPage: "Page", tplDone: "Articles found: {p}, participants: {u} ✓",
    tplErr: "Extraction error: ",
    participantsLabel: "Campaign participants — one username per line",
    participantsPh: "Username 1\nUsername 2\n(or use template extraction above)",
    formulaLabel: "Ranking formula for the jury",
    formulaHint: "Variables:", vBytes: "bytes added", vEdits: "edits", vArticles: "pages", vQuality: "average jury score",
    saveChanges: "Save changes", createContest: "Create contest", cancel: "Cancel",
    contests: "Contests", noContests: "No contests yet — create the first one on the left.",
    participantsN: "participants", tpl: "template", edit: "Edit", del: "Delete",
    saved: "Saved ✓",
    errName: "Enter a contest name", errProjects: "Add at least one project link", errUrl: "Invalid link: ",
    fEmpty: "Formula is empty",
    fChars: "Only numbers, brackets, + − * / and variables are allowed: ",
    fNaN: "Formula does not return a number", fSyntax: "Syntax error in formula",
    addUser: "Add account", role: "Role",
    nameVisible: "Name (visible in comments)",
    userCreated: "Account created ✓",
    errUserReq: "Login and password are required", errUserDup: "This login already exists",
    accounts: "Accounts", create: "Create",
    chPass: "Change password", newPass: "New password", again: "Repeat",
    passShort: "Password too short (min 4 characters)", passDiff: "Passwords don't match", passOk: "Password changed ✓", save: "Save",
    mode: "Analysis mode", mQuant: "Quantitative", mQual: "Qualitative",
    qualAuto: "Qualitative analysis automatically shows quantitative contribution too.",
    modesHint: "Modes can be enabled separately or together.",
    check: "Check contributions", checking: "Checking…", exportCsv: "Export CSV ↓",
    thUser: "Participant", thBytes: "Bytes added", thEdits: "Edits / pages",
    thMy: "My score", thAvg: "Avg quality", thScore: "Formula score",
    scores: "score(s)", comments: "Comments", noComments: "No comments yet.",
    commentPh: "Your comment on this participant's contribution…",
    error: "error",
    noParticipants: "No participants in this contest yet — an admin can add them in settings.",
    formulaOf: "Contest formula:", formulaNote: "the score recalculates automatically after checking contributions and entering jury scores.",
    noContest: "No contests exist.", createIn: "Create one in “Settings”.", askAdmin: "Contact an administrator.",
    footer: "Contest data, scores and comments are kept in shared storage and visible to every user of this tool. Quantitative contribution is computed via the MediaWiki API as the sum of added bytes (positive size changes).",
    loading: "Loading…",
    scoreFor: "Score for ",
    csvN: "#", csvUser: "Participant", csvBytesTotal: "Bytes added (total)", csvBytes: "Bytes: ",
    csvEdits: "Edits", csvArticles: "Pages", csvAvg: "Avg quality", csvCnt: "Scores count",
    csvScore: "Formula score", csvComments: "Comments",
  },
};

// простий хеш пароля (демонстраційний рівень захисту)
const hash = (s) => {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return "h" + (h >>> 0).toString(36) + s.length;
};

async function sGet(key, fallback, shared = true) {
  try {
    const r = await window.storage.get(key, shared);
    return r ? JSON.parse(r.value) : fallback;
  } catch {
    return fallback;
  }
}
async function sSet(key, val, shared = true) {
  try {
    await window.storage.set(key, JSON.stringify(val), shared);
    return true;
  } catch (e) {
    console.error("storage", e);
    return false;
  }
}

// ── MediaWiki API ────────────────────────────────────────────
function projectInfo(url) {
  try {
    const u = new URL(url.trim());
    return {
      origin: u.origin,
      api: u.origin + "/w/api.php",
      label: u.hostname.replace(".org", ""),
      host: u.hostname,
    };
  } catch {
    return null;
  }
}

async function fetchContribs(api, user, startISO, endISO) {
  let uccontinue = null;
  const all = [];
  let guard = 0;
  do {
    const p = new URLSearchParams({
      action: "query",
      list: "usercontribs",
      ucuser: user,
      uclimit: "500",
      ucprop: "title|timestamp|sizediff|ids",
      format: "json",
      origin: "*",
    });
    if (startISO) p.set("ucend", startISO); // API йде від нових до старих
    if (endISO) p.set("ucstart", endISO);
    if (uccontinue) p.set("uccontinue", uccontinue);
    const res = await fetch(api + "?" + p.toString());
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (data.error) throw new Error(data.error.info || "API error");
    all.push(...(data.query?.usercontribs || []));
    uccontinue = data.continue?.uccontinue || null;
    guard++;
  } while (uccontinue && guard < 10);
  return all;
}

// назва шаблону з посилання: https://…/wiki/Шаблон:X → { api, title }
function templateTitleFromUrl(url) {
  try {
    const u = new URL(url.trim());
    const m = u.pathname.match(/\/wiki\/(.+)$/);
    if (!m) return null;
    return { api: u.origin + "/w/api.php", title: decodeURIComponent(m[1]).replace(/_/g, " ") };
  } catch {
    return null;
  }
}

// назва простору назв обговорень для даного проєкту (кешується за api)
const _talkNsCache = {};
async function talkNamespaceName(api) {
  if (_talkNsCache[api]) return _talkNsCache[api];
  const p = new URLSearchParams({
    action: "query", meta: "siteinfo", siprop: "namespaces",
    format: "json", origin: "*",
  });
  const res = await fetch(api + "?" + p.toString());
  const d = await res.json();
  const name = d.query?.namespaces?.["1"]?.["*"] || "Talk";
  _talkNsCache[api] = name;
  return name;
}

// сторінки, що включають шаблон (учасницькі статті конкурсу).
// Шаблон конкурсу нерідко розміщують на сторінці обговорення статті,
// а не на самій статті — тому шукаємо в обох просторах назв (0 і 1),
// а знайдені сторінки обговорення перетворюємо на відповідні статті.
async function fetchTemplatePages(api, title) {
  let cont = null, guard = 0;
  const raw = [];
  do {
    const p = new URLSearchParams({
      action: "query", list: "embeddedin", eititle: title,
      eilimit: "500", einamespace: "0|1", format: "json", origin: "*",
    });
    if (cont) p.set("eicontinue", cont);
    const res = await fetch(api + "?" + p.toString());
    if (!res.ok) throw new Error("HTTP " + res.status);
    const d = await res.json();
    if (d.error) throw new Error(d.error.info || "API error");
    raw.push(...(d.query?.embeddedin || []));
    cont = d.continue?.eicontinue || null;
    guard++;
  } while (cont && guard < 10);
  const talkPrefix = (await talkNamespaceName(api)) + ":";
  const pages = new Set();
  for (const x of raw) {
    pages.add(x.ns === 1 ? x.title.slice(talkPrefix.length) : x.title);
  }
  return [...pages];
}

// автори редагувань сторінки в межах періоду
async function fetchPageEditors(api, title, startISO, endISO) {
  const p = new URLSearchParams({
    action: "query", prop: "revisions", titles: title,
    rvprop: "user|timestamp", rvlimit: "500", format: "json", origin: "*",
  });
  if (startISO) p.set("rvend", startISO);
  if (endISO) p.set("rvstart", endISO);
  const res = await fetch(api + "?" + p.toString());
  if (!res.ok) throw new Error("HTTP " + res.status);
  const d = await res.json();
  const page = Object.values(d.query?.pages || {})[0];
  return (page?.revisions || []).map((r) => r.user).filter(Boolean);
}

// ── Формула ──────────────────────────────────────────────────
const FORMULA_VARS = ["bytes", "edits", "articles", "quality"];
function validateFormula(f, t) {
  if (!f || !f.trim()) return t.fEmpty;
  const cleaned = f.replace(/\b(bytes|edits|articles|quality)\b/g, "1");
  if (!/^[\d\s+\-*/().,]+$/.test(cleaned)) return t.fChars + FORMULA_VARS.join(", ");
  try {
    // eslint-disable-next-line no-new-func
    const fn = new Function(...FORMULA_VARS, "return (" + f + ");");
    const v = fn(1, 1, 1, 1);
    if (typeof v !== "number" || Number.isNaN(v)) return t.fNaN;
    return null;
  } catch {
    return t.fSyntax;
  }
}
function evalFormula(f, vars) {
  try {
    // eslint-disable-next-line no-new-func
    const fn = new Function(...FORMULA_VARS, "return (" + f + ");");
    const v = fn(vars.bytes, vars.edits, vars.articles, vars.quality);
    return typeof v === "number" && !Number.isNaN(v) ? v : null;
  } catch {
    return null;
  }
}

const fmt = (n, lang) => (n == null ? "—" : n.toLocaleString(lang === "uk" ? "uk-UA" : "en-GB"));

// ── Стилі ────────────────────────────────────────────────────
const css = `
:root{
  --ink:#101820; --ink2:#3d4852; --mut:#7a8691;
  --paper:#f2f4f6; --card:#ffffff; --line:#dde3e8;
  --acc:#2748c6; --acc-soft:#e8edfb; --gold:#b07a1e; --gold-soft:#fbf3e2;
  --ok:#1d7a4f; --err:#b3261e;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
.wr{min-height:100vh;background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.5}
.wr input,.wr select,.wr textarea{font:inherit;color:inherit;background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%}
.wr input:focus,.wr select:focus,.wr textarea:focus{outline:2px solid var(--acc);outline-offset:1px;border-color:var(--acc)}
.wr button{font:inherit;cursor:pointer;border:none;border-radius:8px;padding:8px 14px}
.btn-p{background:var(--acc);color:#fff;font-weight:600}
.btn-p:hover{background:#1e3aa8}
.btn-p:disabled{background:#9daed9;cursor:default}
.btn-s{background:#fff;border:1px solid var(--line);color:var(--ink2)}
.btn-s:hover{border-color:var(--acc);color:var(--acc)}
.btn-s:disabled{color:var(--mut);border-color:var(--line);cursor:default}
.btn-d{background:#fff;border:1px solid #eacdcb;color:var(--err)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.lbl{display:block;font-size:12px;font-weight:600;color:var(--ink2);margin:0 0 4px;letter-spacing:.02em}
.hint{font-size:12px;color:var(--mut)}
.mono{font-family:var(--mono)}
.chip{display:inline-block;font-family:var(--mono);font-size:12px;background:var(--acc-soft);color:var(--acc);border-radius:6px;padding:2px 8px}
.tag-a{background:#101820;color:#f5d789}
.tag-j{background:var(--acc-soft);color:var(--acc)}
.tbl{width:100%;border-collapse:collapse}
.tbl th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);text-align:left;padding:8px 10px;border-bottom:2px solid var(--line);white-space:nowrap}
.tbl td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
.tbl tr:hover td{background:#fafbfc}
.bar{height:5px;background:var(--acc-soft);border-radius:3px;overflow:hidden;min-width:70px}
.bar>i{display:block;height:100%;background:var(--acc)}
.pbar{height:8px;background:var(--acc-soft);border-radius:4px;overflow:hidden;width:100%}
.pbar>i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),#5a7be0)}
a.wl{color:var(--acc);text-decoration:none;font-weight:600}
a.wl:hover{text-decoration:underline}
.mode{display:flex;gap:0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}
.mode button{border-radius:0;background:#fff;color:var(--ink2);border-right:1px solid var(--line);padding:9px 16px}
.mode button:last-child{border-right:none}
.mode button.on{background:var(--ink);color:#fff;font-weight:600}
.langsw{display:flex;border:1px solid #33424f;border-radius:8px;overflow:hidden}
.langsw button{border-radius:0;padding:5px 10px;background:transparent;color:#cfd8e0;font-size:12px;font-weight:700}
.langsw button.on{background:#f5d789;color:#101820}
.langsw.light{border-color:var(--line)}
.langsw.light button{color:var(--ink2)}
.langsw.light button.on{background:var(--ink);color:#fff}
@media(prefers-reduced-motion:no-preference){.bar>i,.pbar>i{transition:width .4s ease}}
`;

function ProgressBar({ done, total, label }) {
  if (!total) return null;
  const pct = Math.round((done / total) * 100);
  return (
    <div style={{ marginTop: 6 }}>
      <div className="pbar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <i style={{ width: pct + "%" }} />
      </div>
      <div className="hint mono" style={{ marginTop: 3, fontSize: 11 }}>
        {done} / {total} · {pct}% {label ? "· " + label : ""}
      </div>
    </div>
  );
}

function LangSwitch({ lang, setLang, light }) {
  return (
    <div className={"langsw" + (light ? " light" : "")} role="group" aria-label="Language">
      <button className={lang === "uk" ? "on" : ""} onClick={() => setLang("uk")}>УКР</button>
      <button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>ENG</button>
    </div>
  );
}

// ── Вхід ─────────────────────────────────────────────────────
function Login({ users, onLogin, firstRun, t, lang, setLang }) {
  const [login, setLogin] = useState("");
  const [pass, setPass] = useState("");
  const [err, setErr] = useState("");
  const submit = () => {
    const u = users.find((x) => x.login === login.trim());
    if (!u || u.pass !== hash(pass)) {
      setErr(t.badCreds);
      return;
    }
    onLogin(u);
  };
  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 20 }}>
      <div className="card" style={{ width: 380, maxWidth: "100%" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18 }}>
          <div>
            <div className="mono" style={{ fontSize: 12, color: "var(--gold)", letterSpacing: ".12em" }}>
              WIKI·CONTEST
            </div>
            <h1 style={{ margin: "4px 0 2px", fontSize: 26 }}>{t.brand}</h1>
          </div>
          <LangSwitch lang={lang} setLang={setLang} light />
        </div>
        <div className="hint" style={{ marginBottom: 14 }}>{t.tagline}</div>
        <label className="lbl">{t.login}</label>
        <input value={login} onChange={(e) => setLogin(e.target.value)} autoFocus />
        <label className="lbl" style={{ marginTop: 12 }}>{t.password}</label>
        <input type="password" value={pass} onChange={(e) => setPass(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()} />
        {err && <div style={{ color: "var(--err)", fontSize: 13, marginTop: 8 }}>{err}</div>}
        <button className="btn-p" style={{ width: "100%", marginTop: 16 }} onClick={submit}>
          {t.signin}
        </button>
        {firstRun && (
          <div style={{ marginTop: 14, background: "var(--gold-soft)", borderRadius: 8, padding: "8px 10px", fontSize: 12 }}>
            {t.firstRun.split("{c}")[0]}<b className="mono">admin / admin</b>{t.firstRun.split("{c}")[1]}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Панель адміна ────────────────────────────────────────────
function AdminPanel({ users, setUsers, contests, setContests, me, t, lang }) {
  const [tab, setTab] = useState("contests");
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="mode" style={{ width: "fit-content" }}>
        {[["contests", t.tabContests], ["jury", t.tabUsers], ["security", t.tabPassword]].map(([id, tt]) => (
          <button key={id} className={tab === id ? "on" : ""} onClick={() => setTab(id)}>{tt}</button>
        ))}
      </div>
      {tab === "contests" && <ContestAdmin contests={contests} setContests={setContests} t={t} lang={lang} />}
      {tab === "jury" && <UsersAdmin users={users} setUsers={setUsers} me={me} t={t} />}
      {tab === "security" && <PasswordAdmin users={users} setUsers={setUsers} me={me} t={t} />}
    </div>
  );
}

function ContestAdmin({ contests, setContests, t }) {
  const empty = { name: "", template: "", projects: "https://uk.wikivoyage.org/wiki/Головна_сторінка\nhttps://uk.wikiquote.org/wiki/Головна_сторінка", participants: "", formula: "bytes / 1000 + quality * 10", start: "", end: "" };
  const [draft, setDraft] = useState(empty);
  const [editId, setEditId] = useState(null);
  const [msg, setMsg] = useState("");
  const [extracting, setExtracting] = useState(false);
  const [extLabel, setExtLabel] = useState("");
  const [extDone, setExtDone] = useState(0);
  const [extTotal, setExtTotal] = useState(0);

  const startEdit = (c) => {
    setEditId(c.id);
    setDraft({
      name: c.name,
      template: c.template || "",
      projects: c.projects.join("\n"),
      participants: c.participants.join("\n"),
      formula: c.formula,
      start: c.start || "",
      end: c.end || "",
    });
    setMsg("");
  };

  const extract = async () => {
    const tp = templateTitleFromUrl(draft.template);
    if (!tp) return setMsg(t.tplBad);
    setExtracting(true); setMsg(""); setExtDone(0); setExtTotal(0);
    try {
      const startISO = draft.start ? new Date(draft.start + "T00:00:00Z").toISOString() : null;
      const endISO = draft.end ? new Date(draft.end + "T23:59:59Z").toISOString() : null;
      setExtLabel(t.tplSearch);
      const pages = await fetchTemplatePages(tp.api, tp.title);
      if (!pages.length) {
        setMsg(t.tplNone);
        setExtracting(false); setExtLabel("");
        return;
      }
      setExtTotal(pages.length);
      const users = new Set();
      for (let i = 0; i < pages.length; i++) {
        setExtLabel(pages[i]);
        const editors = await fetchPageEditors(tp.api, pages[i], startISO, endISO);
        editors.forEach((u) => {
          if (!/bot$|бот$/i.test(u.trim())) users.add(u);
        });
        setExtDone(i + 1);
      }
      const existing = draft.participants.split("\n").map((s) => s.trim()).filter(Boolean);
      const merged = [...new Set([...existing, ...users])].sort((a, b) => a.localeCompare(b, "uk"));
      setDraft((d) => ({ ...d, participants: merged.join("\n") }));
      setMsg(t.tplDone.replace("{p}", pages.length).replace("{u}", users.size));
    } catch (e) {
      setMsg(t.tplErr + (e.message || e));
    }
    setExtracting(false); setExtLabel(""); setExtTotal(0); setExtDone(0);
  };

  const save = async () => {
    const projects = draft.projects.split("\n").map((s) => s.trim()).filter(Boolean);
    const bad = projects.find((p) => !projectInfo(p));
    if (!draft.name.trim()) return setMsg(t.errName);
    if (!projects.length) return setMsg(t.errProjects);
    if (bad) return setMsg(t.errUrl + bad);
    const fErr = validateFormula(draft.formula, t);
    if (fErr) return setMsg(fErr);
    const participants = draft.participants.split("\n").map((s) => s.trim()).filter(Boolean);
    const item = {
      id: editId || "c" + Date.now().toString(36),
      name: draft.name.trim(),
      template: draft.template.trim(),
      projects, participants,
      formula: draft.formula.trim(),
      start: draft.start, end: draft.end,
    };
    const next = editId ? contests.map((c) => (c.id === editId ? item : c)) : [...contests, item];
    setContests(next);
    await sSet(K_CONTESTS, next);
    setDraft(empty); setEditId(null);
    setMsg(t.saved);
  };
  const remove = async (id) => {
    const next = contests.filter((c) => c.id !== id);
    setContests(next);
    await sSet(K_CONTESTS, next);
    try { await window.storage.delete(kAssess(id), true); } catch {}
    try { await window.storage.delete(kResults(id), true); } catch {}
  };

  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(320px,1fr) minmax(280px,360px)" }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>{editId ? t.editContest : t.newContest}</h3>
        <label className="lbl">{t.contestName}</label>
        <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          placeholder="Cultural Heritage 2026" />
        <label className="lbl" style={{ marginTop: 12 }}>{t.projectsLabel}</label>
        <textarea rows={3} className="mono" style={{ fontSize: 12 }} value={draft.projects}
          onChange={(e) => setDraft({ ...draft, projects: e.target.value })} />
        <div className="hint">{t.projectsHint}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 12 }}>
          <div>
            <label className="lbl">{t.dateStart}</label>
            <input type="date" value={draft.start} onChange={(e) => setDraft({ ...draft, start: e.target.value })} />
          </div>
          <div>
            <label className="lbl">{t.dateEnd}</label>
            <input type="date" value={draft.end} onChange={(e) => setDraft({ ...draft, end: e.target.value })} />
          </div>
        </div>
        <div style={{ marginTop: 12, background: "var(--acc-soft)", borderRadius: 10, padding: "10px 12px" }}>
          <label className="lbl">{t.tplLabel}</label>
          <input className="mono" style={{ fontSize: 12 }} value={draft.template}
            onChange={(e) => setDraft({ ...draft, template: e.target.value })}
            placeholder="https://uk.wikivoyage.org/wiki/Шаблон:Cultural_Heritage_and_Notable_Personalities_2026" />
          <div style={{ marginTop: 8 }}>
            <button className="btn-p" onClick={extract} disabled={extracting}>
              {extracting ? t.tplExtracting : t.tplExtract}
            </button>
            {extracting && !extTotal && <span className="hint mono" style={{ marginLeft: 10, fontSize: 11 }}>{extLabel}</span>}
            {extracting && extTotal > 0 && <ProgressBar done={extDone} total={extTotal} label={extLabel} />}
          </div>
          <div className="hint" style={{ marginTop: 6 }}>{t.tplHint}</div>
        </div>
        <label className="lbl" style={{ marginTop: 12 }}>{t.participantsLabel}</label>
        <textarea rows={5} value={draft.participants}
          onChange={(e) => setDraft({ ...draft, participants: e.target.value })}
          placeholder={t.participantsPh} />
        <label className="lbl" style={{ marginTop: 12 }}>{t.formulaLabel}</label>
        <input className="mono" value={draft.formula} onChange={(e) => setDraft({ ...draft, formula: e.target.value })} />
        <div className="hint">
          {t.formulaHint} <span className="chip">bytes</span> {t.vBytes}, <span className="chip">edits</span> {t.vEdits},{" "}
          <span className="chip">articles</span> {t.vArticles}, <span className="chip">quality</span> {t.vQuality}.
        </div>
        {msg && <div style={{ marginTop: 10, color: msg.includes("✓") ? "var(--ok)" : "var(--err)", fontWeight: 600 }}>{msg}</div>}
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button className="btn-p" onClick={save}>{editId ? t.saveChanges : t.createContest}</button>
          {editId && <button className="btn-s" onClick={() => { setEditId(null); setDraft(empty); setMsg(""); }}>{t.cancel}</button>}
        </div>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>{t.contests} ({contests.length})</h3>
        {!contests.length && <div className="hint">{t.noContests}</div>}
        {contests.map((c) => (
          <div key={c.id} style={{ borderBottom: "1px solid var(--line)", padding: "10px 0" }}>
            <div style={{ fontWeight: 700 }}>{c.name}</div>
            <div className="hint">
              {c.projects.map((p) => projectInfo(p)?.label).join(" · ")} · {t.participantsN}: {c.participants.length}
            </div>
            {c.template && (
              <div className="hint mono" style={{ fontSize: 11 }}>
                {t.tpl}: {templateTitleFromUrl(c.template)?.title || c.template}
              </div>
            )}
            <div className="mono hint" style={{ margin: "4px 0" }}>{c.formula}</div>
            <div style={{ display: "flex", gap: 6 }}>
              <button className="btn-s" style={{ padding: "4px 10px", fontSize: 12 }} onClick={() => startEdit(c)}>{t.edit}</button>
              <button className="btn-d" style={{ padding: "4px 10px", fontSize: 12 }} onClick={() => remove(c.id)}>{t.del}</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function UsersAdmin({ users, setUsers, me, t }) {
  const [d, setD] = useState({ login: "", pass: "", name: "", role: "jury" });
  const [msg, setMsg] = useState("");
  const add = async () => {
    if (!d.login.trim() || !d.pass) return setMsg(t.errUserReq);
    if (users.some((u) => u.login === d.login.trim())) return setMsg(t.errUserDup);
    const next = [...users, { login: d.login.trim(), pass: hash(d.pass), name: d.name.trim() || d.login.trim(), role: d.role }];
    setUsers(next);
    await sSet(K_USERS, next);
    setD({ login: "", pass: "", name: "", role: "jury" });
    setMsg(t.userCreated);
  };
  const remove = async (login) => {
    if (login === me.login) return;
    const next = users.filter((u) => u.login !== login);
    setUsers(next);
    await sSet(K_USERS, next);
  };
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(300px,420px) 1fr" }}>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>{t.addUser}</h3>
        <label className="lbl">{t.role}</label>
        <select value={d.role} onChange={(e) => setD({ ...d, role: e.target.value })}>
          <option value="jury">{t.jury}</option>
          <option value="admin">{t.admin}</option>
        </select>
        <label className="lbl" style={{ marginTop: 10 }}>{t.nameVisible}</label>
        <input value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} />
        <label className="lbl" style={{ marginTop: 10 }}>{t.login}</label>
        <input value={d.login} onChange={(e) => setD({ ...d, login: e.target.value })} />
        <label className="lbl" style={{ marginTop: 10 }}>{t.password}</label>
        <input type="password" value={d.pass} onChange={(e) => setD({ ...d, pass: e.target.value })} />
        {msg && <div style={{ marginTop: 8, color: msg.includes("✓") ? "var(--ok)" : "var(--err)", fontWeight: 600 }}>{msg}</div>}
        <button className="btn-p" style={{ marginTop: 12 }} onClick={add}>{t.create}</button>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>{t.accounts} ({users.length})</h3>
        {users.map((u) => (
          <div key={u.login} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 0", borderBottom: "1px solid var(--line)" }}>
            <span className={"chip " + (u.role === "admin" ? "tag-a" : "tag-j")}>{u.role === "admin" ? t.admin : t.jury}</span>
            <b>{u.name}</b>
            <span className="mono hint">{u.login}</span>
            {u.login !== me.login && (
              <button className="btn-d" style={{ marginLeft: "auto", padding: "3px 10px", fontSize: 12 }} onClick={() => remove(u.login)}>
                {t.del}
              </button>
            )}
            {u.login === me.login && <span className="hint" style={{ marginLeft: "auto" }}>{t.youAre}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

function PasswordAdmin({ users, setUsers, me, t }) {
  const [p1, setP1] = useState(""); const [p2, setP2] = useState(""); const [msg, setMsg] = useState("");
  const save = async () => {
    if (p1.length < 4) return setMsg(t.passShort);
    if (p1 !== p2) return setMsg(t.passDiff);
    const next = users.map((u) => (u.login === me.login ? { ...u, pass: hash(p1) } : u));
    setUsers(next);
    await sSet(K_USERS, next);
    setP1(""); setP2(""); setMsg(t.passOk);
  };
  return (
    <div className="card" style={{ maxWidth: 400 }}>
      <h3 style={{ marginTop: 0 }}>{t.chPass} — {me.name}</h3>
      <label className="lbl">{t.newPass}</label>
      <input type="password" value={p1} onChange={(e) => setP1(e.target.value)} />
      <label className="lbl" style={{ marginTop: 10 }}>{t.again}</label>
      <input type="password" value={p2} onChange={(e) => setP2(e.target.value)} />
      {msg && <div style={{ marginTop: 8, color: msg.includes("✓") ? "var(--ok)" : "var(--err)", fontWeight: 600 }}>{msg}</div>}
      <button className="btn-p" style={{ marginTop: 12 }} onClick={save}>{t.save}</button>
    </div>
  );
}

// ── Аналіз ───────────────────────────────────────────────────
function Analysis({ contest, me, t, lang }) {
  const [quant, setQuant] = useState(true);
  const [qual, setQual] = useState(false);
  const [results, setResults] = useState(null);
  const [assess, setAssess] = useState({});
  const [running, setRunning] = useState(false);
  const [runDone, setRunDone] = useState(0);
  const [runLabel, setRunLabel] = useState("");
  const [drafts, setDrafts] = useState({});
  const [expanded, setExpanded] = useState(null);

  const projects = useMemo(() => contest.projects.map(projectInfo).filter(Boolean), [contest]);

  useEffect(() => {
    (async () => {
      setAssess(await sGet(kAssess(contest.id), {}));
      setResults(await sGet(kResults(contest.id), null));
      setExpanded(null); setDrafts({});
    })();
  }, [contest.id]);

  const toggleQual = () => {
    const v = !qual;
    setQual(v);
    if (v) setQuant(true);
  };
  const toggleQuant = () => {
    if (qual) return;
    setQuant(!quant);
  };

  const run = async () => {
    setRunning(true); setRunDone(0);
    const startISO = contest.start ? new Date(contest.start + "T00:00:00Z").toISOString() : null;
    const endISO = contest.end ? new Date(contest.end + "T23:59:59Z").toISOString() : null;
    const out = {};
    for (let i = 0; i < contest.participants.length; i++) {
      const user = contest.participants[i];
      setRunLabel(user);
      const row = { perProject: {}, bytes: 0, edits: 0, articles: 0 };
      const titles = new Set();
      for (const pr of projects) {
        try {
          const contribs = await fetchContribs(pr.api, user, startISO, endISO);
          let bytes = 0;
          contribs.forEach((c) => {
            if (c.sizediff > 0) bytes += c.sizediff;
            titles.add(pr.host + "::" + c.title);
          });
          row.perProject[pr.host] = { bytes, edits: contribs.length, articles: new Set(contribs.map((c) => c.title)).size };
          row.bytes += bytes;
          row.edits += contribs.length;
        } catch (e) {
          row.perProject[pr.host] = { err: String(e.message || e) };
        }
      }
      row.articles = titles.size;
      out[user] = row;
      setRunDone(i + 1);
    }
    setResults(out);
    await sSet(kResults(contest.id), out);
    setRunLabel("");
    setRunning(false);
  };

  const saveScore = async (user, val) => {
    const num = val === "" ? null : Number(val);
    const next = { ...assess, [user]: { ...(assess[user] || {}), scores: { ...((assess[user] || {}).scores || {}), [me.login]: num } } };
    setAssess(next);
    await sSet(kAssess(contest.id), next);
  };
  const saveComment = async (user) => {
    const text = (drafts[user] || "").trim();
    if (!text) return;
    const entry = { jury: me.name, text, date: new Date().toISOString().slice(0, 16).replace("T", " ") };
    const next = { ...assess, [user]: { ...(assess[user] || {}), comments: [...(((assess[user] || {}).comments) || []), entry] } };
    setAssess(next);
    await sSet(kAssess(contest.id), next);
    setDrafts({ ...drafts, [user]: "" });
  };

  const rows = useMemo(() => {
    return contest.participants.map((user) => {
      const r = results?.[user];
      const a = assess[user] || {};
      const scores = Object.values(a.scores || {}).filter((v) => v != null);
      const quality = scores.length ? scores.reduce((x, y) => x + y, 0) / scores.length : 0;
      const score = r
        ? evalFormula(contest.formula, { bytes: r.bytes, edits: r.edits, articles: r.articles, quality })
        : null;
      return { user, r, a, quality, nScores: scores.length, score, myScore: (a.scores || {})[me.login] };
    }).sort((x, y) => (qual ? (y.score ?? -1e18) - (x.score ?? -1e18) : (y.r?.bytes ?? -1) - (x.r?.bytes ?? -1)));
  }, [contest, results, assess, qual, me.login]);

  const maxBytes = Math.max(1, ...rows.map((r) => r.r?.bytes || 0));

  const exportCSV = () => {
    const head = [t.csvN, t.csvUser];
    if (quant) {
      head.push(t.csvBytesTotal);
      projects.forEach((p) => head.push(t.csvBytes + p.label));
      head.push(t.csvEdits, t.csvArticles);
    }
    if (qual) head.push(t.csvAvg, t.csvCnt, t.csvScore, t.csvComments);
    const lines = [head];
    rows.forEach((row, i) => {
      const line = [i + 1, row.user];
      if (quant) {
        line.push(row.r?.bytes ?? "");
        projects.forEach((p) => {
          const pp = row.r?.perProject?.[p.host];
          line.push(pp ? (pp.err ? t.error : pp.bytes) : "");
        });
        line.push(row.r?.edits ?? "", row.r?.articles ?? "");
      }
      if (qual) {
        line.push(
          row.nScores ? row.quality.toFixed(2) : "",
          row.nScores,
          row.score != null ? row.score.toFixed(2) : "",
          (row.a.comments || []).map((c) => `${c.jury} (${c.date}): ${c.text}`).join(" | ")
        );
      }
      lines.push(line);
    });
    const csv = lines
      .map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(";"))
      .join("\r\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = contest.name.replace(/[^\p{L}\p{N} _-]/gu, "").trim().replace(/\s+/g, "_") + ".csv";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // Special:Contributions працює у будь-якому мовному розділі MediaWiki
  const contribLink = (origin, user) => origin + "/wiki/Special:Contributions/" + encodeURIComponent(user);

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="card" style={{ display: "flex", flexWrap: "wrap", gap: 14, alignItems: "center" }}>
        <div>
          <div className="lbl" style={{ margin: 0 }}>{t.mode}</div>
          <div className="mode" style={{ marginTop: 4 }}>
            <button className={quant ? "on" : ""} onClick={toggleQuant} aria-pressed={quant}>{t.mQuant}</button>
            <button className={qual ? "on" : ""} onClick={toggleQual} aria-pressed={qual}>{t.mQual}</button>
          </div>
          <div className="hint" style={{ marginTop: 4 }}>
            {qual ? t.qualAuto : t.modesHint}
          </div>
        </div>
        <div style={{ marginLeft: "auto", textAlign: "right", minWidth: 260 }}>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="btn-s" onClick={exportCSV} disabled={!rows.length}>
              {t.exportCsv}
            </button>
            <button className="btn-p" onClick={run} disabled={running}>
              {running ? t.checking : t.check}
            </button>
          </div>
          {running && <ProgressBar done={runDone} total={contest.participants.length} label={runLabel} />}
        </div>
      </div>

      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table className="tbl">
          <thead>
            <tr>
              <th>#</th>
              <th>{t.thUser}</th>
              {quant && <th>{t.thBytes}</th>}
              {quant && projects.map((p) => <th key={p.host}>{p.label}</th>)}
              {quant && <th>{t.thEdits}</th>}
              {qual && <th>{t.thMy}</th>}
              {qual && <th>{t.thAvg}</th>}
              {qual && <th>{t.thScore}</th>}
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <React.Fragment key={row.user}>
                <tr>
                  <td className="mono" style={{ color: "var(--mut)" }}>{i + 1}</td>
                  <td>
                    <a className="wl" target="_blank" rel="noopener noreferrer"
                      href={contribLink(projects[0]?.origin, row.user)}>
                      {row.user} ↗
                    </a>
                    <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
                      {projects.map((p) => (
                        <a key={p.host} className="hint" style={{ color: "var(--mut)" }} target="_blank" rel="noopener noreferrer"
                          href={contribLink(p.origin, row.user)}>
                          {p.label} ↗
                        </a>
                      ))}
                    </div>
                  </td>
                  {quant && (
                    <td>
                      <div className="mono" style={{ fontWeight: 700 }}>
                        {row.r ? "+" + fmt(row.r.bytes, lang) + (lang === "uk" ? " Б" : " B") : "—"}
                      </div>
                      <div className="bar" style={{ marginTop: 4 }}>
                        <i style={{ width: ((row.r?.bytes || 0) / maxBytes) * 100 + "%" }} />
                      </div>
                    </td>
                  )}
                  {quant && projects.map((p) => {
                    const pp = row.r?.perProject?.[p.host];
                    return (
                      <td key={p.host} className="mono" style={{ fontSize: 12 }}>
                        {pp ? (pp.err ? <span style={{ color: "var(--err)" }} title={pp.err}>{t.error}</span> : "+" + fmt(pp.bytes, lang)) : "—"}
                      </td>
                    );
                  })}
                  {quant && (
                    <td className="mono" style={{ fontSize: 12 }}>
                      {row.r ? fmt(row.r.edits, lang) + " / " + fmt(row.r.articles, lang) : "—"}
                    </td>
                  )}
                  {qual && (
                    <td>
                      <input type="number" step="0.1" style={{ width: 74 }} value={row.myScore ?? ""}
                        onChange={(e) => saveScore(row.user, e.target.value)} aria-label={t.scoreFor + row.user} />
                    </td>
                  )}
                  {qual && (
                    <td className="mono">
                      {row.nScores ? row.quality.toFixed(2) : "—"}
                      <div className="hint">{row.nScores} {t.scores}</div>
                    </td>
                  )}
                  {qual && (
                    <td>
                      <span className="mono" style={{ fontWeight: 700, color: "var(--gold)", fontSize: 15 }}>
                        {row.score != null ? row.score.toFixed(2) : "—"}
                      </span>
                    </td>
                  )}
                  <td>
                    {qual && (
                      <button className="btn-s" style={{ padding: "4px 10px", fontSize: 12 }}
                        onClick={() => setExpanded(expanded === row.user ? null : row.user)}>
                        {t.comments}{row.a.comments?.length ? ` (${row.a.comments.length})` : ""}
                      </button>
                    )}
                  </td>
                </tr>
                {qual && expanded === row.user && (
                  <tr>
                    <td colSpan={99} style={{ background: "#fafbfc" }}>
                      <div style={{ maxWidth: 640 }}>
                        {(row.a.comments || []).map((c, j) => (
                          <div key={j} style={{ padding: "6px 0", borderBottom: "1px dashed var(--line)" }}>
                            <b>{c.jury}</b> <span className="hint mono">{c.date}</span>
                            <div>{c.text}</div>
                          </div>
                        ))}
                        {!row.a.comments?.length && <div className="hint">{t.noComments}</div>}
                        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                          <input placeholder={t.commentPh} value={drafts[row.user] || ""}
                            onChange={(e) => setDrafts({ ...drafts, [row.user]: e.target.value })}
                            onKeyDown={(e) => e.key === "Enter" && saveComment(row.user)} />
                          <button className="btn-p" onClick={() => saveComment(row.user)}>{t.save}</button>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
            {!rows.length && (
              <tr><td colSpan={99} className="hint" style={{ padding: 20 }}>{t.noParticipants}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {qual && (
        <div className="hint">
          {t.formulaOf} <span className="chip">{contest.formula}</span> · {t.formulaNote}
        </div>
      )}
    </div>
  );
}

// ── Головний застосунок ──────────────────────────────────────
export default function App() {
  const [ready, setReady] = useState(false);
  const [users, setUsers] = useState([]);
  const [contests, setContests] = useState([]);
  const [me, setMe] = useState(null);
  const [firstRun, setFirstRun] = useState(false);
  const [view, setView] = useState("analysis");
  const [contestId, setContestId] = useState(null);
  const [lang, setLangState] = useState("uk");
  const t = T[lang];

  const setLang = (l) => {
    setLangState(l);
    sSet("wr_lang", l, false); // особисте налаштування, не спільне
  };

  useEffect(() => {
    (async () => {
      const savedLang = await sGet("wr_lang", "uk", false);
      if (savedLang === "en" || savedLang === "uk") setLangState(savedLang);
      let u = await sGet(K_USERS, null);
      if (!u || !u.length) {
        u = [{ login: "admin", pass: hash("admin"), name: "Admin", role: "admin" }];
        await sSet(K_USERS, u);
        setFirstRun(true);
      }
      setUsers(u);
      const c = await sGet(K_CONTESTS, []);
      setContests(c);
      if (c.length) setContestId(c[0].id);
      setReady(true);
    })();
  }, []);

  useEffect(() => {
    if (contests.length && !contests.some((c) => c.id === contestId)) setContestId(contests[0].id);
    if (!contests.length) setContestId(null);
  }, [contests, contestId]);

  if (!ready) return <div className="wr" style={{ display: "grid", placeItems: "center", minHeight: "100vh" }}><style>{css}</style><div className="hint">{t.loading}</div></div>;

  if (!me) return <div className="wr"><style>{css}</style><Login users={users} firstRun={firstRun} onLogin={setMe} t={t} lang={lang} setLang={setLang} /></div>;

  const contest = contests.find((c) => c.id === contestId) || null;

  return (
    <div className="wr">
      <style>{css}</style>
      <header style={{ background: "var(--ink)", color: "#fff", padding: "12px 20px", display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <div>
          <span className="mono" style={{ fontSize: 11, color: "#f5d789", letterSpacing: ".12em" }}>WIKI·CONTEST</span>
          <div style={{ fontWeight: 800, fontSize: 18, lineHeight: 1.1 }}>{t.brand}</div>
        </div>
        {contests.length > 0 && (
          <select value={contestId || ""} onChange={(e) => setContestId(e.target.value)}
            style={{ width: "auto", background: "#1d2a36", color: "#fff", border: "1px solid #33424f" }}>
            {contests.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        <nav style={{ display: "flex", gap: 6 }}>
          <button className="btn-s" style={{ background: view === "analysis" ? "#fff" : "transparent", color: view === "analysis" ? "var(--ink)" : "#cfd8e0", borderColor: "#33424f" }}
            onClick={() => setView("analysis")}>{t.navAnalysis}</button>
          {me.role === "admin" && (
            <button className="btn-s" style={{ background: view === "admin" ? "#fff" : "transparent", color: view === "admin" ? "var(--ink)" : "#cfd8e0", borderColor: "#33424f" }}
              onClick={() => setView("admin")}>{t.navSettings}</button>
          )}
        </nav>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          <LangSwitch lang={lang} setLang={setLang} />
          <span className={"chip " + (me.role === "admin" ? "tag-a" : "tag-j")}>{me.role === "admin" ? t.admin : t.jury}</span>
          <b>{me.name}</b>
          <button className="btn-s" style={{ background: "transparent", color: "#cfd8e0", borderColor: "#33424f" }} onClick={() => setMe(null)}>{t.logout}</button>
        </div>
      </header>

      <main style={{ padding: 20, maxWidth: 1200, margin: "0 auto" }}>
        {view === "admin" && me.role === "admin" && (
          <AdminPanel users={users} setUsers={setUsers} contests={contests} setContests={setContests} me={me} t={t} lang={lang} />
        )}
        {view === "analysis" && (
          contest
            ? <Analysis contest={contest} me={me} t={t} lang={lang} key={contest.id + lang} />
            : <div className="card hint">{t.noContest} {me.role === "admin" ? t.createIn : t.askAdmin}</div>
        )}
      </main>
      <footer style={{ padding: "0 20px 20px", maxWidth: 1200, margin: "0 auto" }}>
        <div className="hint">{t.footer}</div>
      </footer>
    </div>
  );
}
