# -*- coding: utf-8 -*-
"""
WikiRank / ВікіРанг — аналіз внеску учасників вікіконкурсів.
Версія для Wikimedia Toolforge (Flask + SQLite).

Ролі: admin (налаштування) та jury (оцінювання).
Кількісний внесок і витягування учасників із шаблону виконуються
у браузері напряму через API MediaWiki (CORS origin=*), а результати
зберігаються на сервері. Мови інтерфейсу: uk / en.
"""
import ast
import csv
import io
import json
import operator
import os
import re
import sqlite3
import time
from functools import wraps
from urllib.parse import quote

from flask import (Flask, g, jsonify, redirect, render_template_string,
                   request, session, url_for, Response)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)

# ── OAuth через MediaWiki (Вікімедіа) — опційно, поряд зі звичайним логіном ──
try:
    import mwoauth
except ImportError:
    mwoauth = None
OAUTH_MWURI = "https://meta.wikimedia.org/w/index.php"
OAUTH_CONSUMER_KEY = os.environ.get("OAUTH_CONSUMER_KEY")
OAUTH_CONSUMER_SECRET = os.environ.get("OAUTH_CONSUMER_SECRET")
OAUTH_ENABLED = bool(mwoauth and OAUTH_CONSUMER_KEY and OAUTH_CONSUMER_SECRET)
DB_PATH = os.environ.get("WIKIRANK_DB",
                         os.path.join(os.path.expanduser("~"), "wikirank.db"))

# ── База даних ───────────────────────────────────────────────
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  login TEXT PRIMARY KEY, name TEXT, pass TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS contests(
  id TEXT PRIMARY KEY, name TEXT, template TEXT, projects TEXT,
  participants TEXT, formula TEXT, start TEXT, end TEXT);
CREATE TABLE IF NOT EXISTS results(
  contest_id TEXT PRIMARY KEY, data TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS scores(
  contest_id TEXT, participant TEXT, jury TEXT, value REAL,
  PRIMARY KEY(contest_id, participant, jury));
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, contest_id TEXT, participant TEXT,
  jury_name TEXT, text TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS article_scores(
  contest_id TEXT, participant TEXT, article TEXT, jury TEXT, value REAL,
  PRIMARY KEY(contest_id, participant, article, jury));
CREATE TABLE IF NOT EXISTS article_comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, contest_id TEXT, participant TEXT, article TEXT,
  jury_name TEXT, text TEXT, date TEXT);
"""

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(contests)")]
    if "tpl_talk_only" not in cols:
        con.execute("ALTER TABLE contests ADD COLUMN tpl_talk_only INTEGER DEFAULT 0")
    cur = con.execute("SELECT COUNT(*) c FROM users")
    if cur.fetchone()[0] == 0:
        con.execute("INSERT INTO users VALUES(?,?,?,?)",
                    ("admin", "Admin", generate_password_hash("admin"), "admin"))
    con.commit()
    con.close()

init_db()

# ── Безпечна формула (AST, без eval) ─────────────────────────
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos,
        ast.Pow: operator.pow}
FORMULA_VARS = ("bytes", "edits", "articles", "quality")

def eval_formula(expr, variables):
    def _ev(node):
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in FORMULA_VARS:
            return float(variables.get(node.id, 0))
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.left), _ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.operand))
        raise ValueError("forbidden node")
    try:
        return _ev(ast.parse(expr, mode="eval"))
    except Exception:
        return None

def validate_formula(expr):
    if not expr or not expr.strip():
        return False
    v = eval_formula(expr, {k: 1 for k in FORMULA_VARS})
    return isinstance(v, (int, float))

# ── i18n ─────────────────────────────────────────────────────
T = {
 "uk": dict(brand="ВікіРанг", tagline="Аналіз внеску учасників вікіпроєктів. Доступ лише для адмінів та журі.",
   login="Логін", password="Пароль", signin="Увійти", bad="Невірний логін або пароль",
   or_="або", oauth_btn="Увійти через MediaWiki",
   oauth_unknown="Ваш вікі-акаунт не зареєстровано в системі як журі чи адмін:",
   admin="адмін", jury="журі", logout="Вийти", nav_a="Оцінювання", nav_s="Налаштування",
   tab_c="Конкурси", tab_u="Журі та адміни", tab_p="Мій пароль",
   new_c="Новий конкурс", edit_c="Редагувати конкурс", c_name="Назва конкурсу",
   projects="Проєкти — по одному посиланню в рядку",
   projects_h="Будь-який мовний розділ MediaWiki: uk.wikivoyage.org, en.wikivoyage.org тощо.",
   d_start="Початок періоду (необов'язково)", d_end="Кінець періоду",
   tpl="Шаблон конкурсу — ідентифікує статті кампанії",
   tpl_btn="Витягнути учасників із шаблону", tpl_run="Витягую…",
   tpl_talk_only="Шаблон розміщено на сторінці обговорення (шукати лише там, а не в статтях)",
   tpl_h="Знайдуться всі статті з шаблоном; автори редагувань у межах дат додадуться до списку (боти відсіюються).",
   parts="Учасники — по одному імені в рядку", formula="Формула ранжування для журі",
   formula_h="Змінні: bytes (байти), edits (редагування), articles (сторінки), quality (сер. оцінка журі)",
   save="Зберегти", create="Створити", cancel="Скасувати", edit="Редагувати", delete="Видалити",
   no_c="Ще немає жодного конкурсу.", parts_n="учасників",
   add_u="Додати обліковий запис", role="Роль", u_name="Ім'я (видиме в коментарях)",
   accounts="Облікові записи", its_you="це ви",
   ch_pass="Зміна пароля", new_pass="Новий пароль", again="Ще раз",
   mode="Режим аналізу", quant="Кількісний", qual="Якісний",
   qual_h="Якісний аналіз автоматично показує й кількісний внесок.",
   modes_h="Режими можна вмикати окремо або разом.",
   check="Перевірити внесок", checking="Перевіряю…", export="Експорт CSV ↓",
   th_u="Учасник", th_b="Додано байтів", th_e="Ред. / стор.", th_my="Моя оцінка",
   th_avg="Сер. якість", th_score="Бал за формулою", scores_n="оцін.",
   comments="Коментарі", no_comments="Коментарів ще немає.",
   comment_ph="Ваш коментар до внеску учасника…", err="помилка",
   no_parts="Учасників ще немає — адмін може додати їх у налаштуваннях.",
   f_of="Формула конкурсу:", saved="Збережено ✓",
   articles_btn="Статті", no_articles="У межах конкурсу немає редагувань статей.",
   created="створено", expanded="доповнено",
   view="Переглянути", open_article="Відкрити статтю", loading="Завантаження…",
   footer="Кількісний внесок = сума доданих байтів через API MediaWiki."),
 "en": dict(brand="WikiRank", tagline="Contribution analysis for wiki contests. Admins and jury only.",
   login="Login", password="Password", signin="Sign in", bad="Wrong login or password",
   or_="or", oauth_btn="Log in with MediaWiki",
   oauth_unknown="Your wiki account is not registered in the system as jury or admin:",
   admin="admin", jury="jury", logout="Log out", nav_a="Assessment", nav_s="Settings",
   tab_c="Contests", tab_u="Jury & admins", tab_p="My password",
   new_c="New contest", edit_c="Edit contest", c_name="Contest name",
   projects="Projects — one link per line",
   projects_h="Any MediaWiki language edition: uk.wikivoyage.org, en.wikivoyage.org, etc.",
   d_start="Period start (optional)", d_end="Period end",
   tpl="Contest template — identifies campaign articles",
   tpl_btn="Extract participants from template", tpl_run="Extracting…",
   tpl_talk_only="Template is placed on talk pages (search only there, not in articles)",
   tpl_h="Finds every article transcluding the template; revision authors within the dates are merged into the list (bots filtered out).",
   parts="Participants — one username per line", formula="Ranking formula for the jury",
   formula_h="Variables: bytes, edits, articles, quality (avg jury score)",
   save="Save", create="Create", cancel="Cancel", edit="Edit", delete="Delete",
   no_c="No contests yet.", parts_n="participants",
   add_u="Add account", role="Role", u_name="Name (visible in comments)",
   accounts="Accounts", its_you="that's you",
   ch_pass="Change password", new_pass="New password", again="Repeat",
   mode="Analysis mode", quant="Quantitative", qual="Qualitative",
   qual_h="Qualitative analysis automatically shows quantitative contribution too.",
   modes_h="Modes can be enabled separately or together.",
   check="Check contributions", checking="Checking…", export="Export CSV ↓",
   th_u="Participant", th_b="Bytes added", th_e="Edits / pages", th_my="My score",
   th_avg="Avg quality", th_score="Formula score", scores_n="score(s)",
   comments="Comments", no_comments="No comments yet.",
   comment_ph="Your comment on this participant's contribution…", err="error",
   no_parts="No participants yet — an admin can add them in settings.",
   f_of="Contest formula:", saved="Saved ✓",
   articles_btn="Articles", no_articles="No article edits within the contest scope.",
   created="created", expanded="expanded",
   view="View", open_article="Open article", loading="Loading…",
   footer="Quantitative contribution = sum of added bytes via the MediaWiki API."),
 "pl": dict(brand="WikiRank", tagline="Analiza wkładu uczestników konkursów wiki. Dostęp tylko dla adminów i jury.",
   login="Login", password="Hasło", signin="Zaloguj się", bad="Nieprawidłowy login lub hasło",
   or_="lub", oauth_btn="Zaloguj się przez MediaWiki",
   oauth_unknown="Twoje konto wiki nie jest zarejestrowane w systemie jako jury lub admin:",
   admin="admin", jury="jury", logout="Wyloguj się", nav_a="Ocenianie", nav_s="Ustawienia",
   tab_c="Konkursy", tab_u="Jury i admini", tab_p="Moje hasło",
   new_c="Nowy konkurs", edit_c="Edytuj konkurs", c_name="Nazwa konkursu",
   projects="Projekty — jeden link w wierszu",
   projects_h="Dowolna wersja językowa MediaWiki: uk.wikivoyage.org, en.wikivoyage.org itd.",
   d_start="Początek okresu (opcjonalnie)", d_end="Koniec okresu",
   tpl="Szablon konkursu — identyfikuje artykuły kampanii",
   tpl_btn="Wyodrębnij uczestników z szablonu", tpl_run="Wyodrębnianie…",
   tpl_talk_only="Szablon umieszczony na stronie dyskusji (szukaj tylko tam, nie w artykułach)",
   tpl_h="Znajdzie wszystkie artykuły z szablonem; autorzy edycji w podanym okresie zostaną dodani do listy (boty są pomijane).",
   parts="Uczestnicy — jedna nazwa użytkownika w wierszu", formula="Formuła rankingowa dla jury",
   formula_h="Zmienne: bytes (bajty), edits (edycje), articles (strony), quality (śr. ocena jury)",
   save="Zapisz", create="Utwórz", cancel="Anuluj", edit="Edytuj", delete="Usuń",
   no_c="Nie ma jeszcze żadnego konkursu.", parts_n="uczestników",
   add_u="Dodaj konto", role="Rola", u_name="Imię (widoczne w komentarzach)",
   accounts="Konta", its_you="to ty",
   ch_pass="Zmiana hasła", new_pass="Nowe hasło", again="Powtórz",
   mode="Tryb analizy", quant="Ilościowy", qual="Jakościowy",
   qual_h="Analiza jakościowa automatycznie pokazuje też wkład ilościowy.",
   modes_h="Tryby można włączać osobno lub razem.",
   check="Sprawdź wkład", checking="Sprawdzanie…", export="Eksport CSV ↓",
   th_u="Uczestnik", th_b="Dodane bajty", th_e="Edycje / strony", th_my="Moja ocena",
   th_avg="Śr. jakość", th_score="Wynik wg formuły", scores_n="ocen",
   comments="Komentarze", no_comments="Jeszcze brak komentarzy.",
   comment_ph="Twój komentarz do wkładu uczestnika…", err="błąd",
   no_parts="Jeszcze brak uczestników — admin może ich dodać w ustawieniach.",
   f_of="Formuła konkursu:", saved="Zapisano ✓",
   articles_btn="Artykuły", no_articles="Brak edycji artykułów w ramach konkursu.",
   created="utworzono", expanded="rozbudowano",
   view="Zobacz", open_article="Otwórz artykuł", loading="Ładowanie…",
   footer="Wkład ilościowy = suma dodanych bajtów przez API MediaWiki."),
}

def lang():
    l = request.args.get("lang") or session.get("lang") or "uk"
    if l not in T:
        l = "uk"
    session["lang"] = l
    return l

# ── Авторизація ──────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "user" not in session:
            return redirect(url_for("login_view"))
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if "user" not in session:
            return redirect(url_for("login_view"))
        if session["user"]["role"] != "admin":
            return "Forbidden", 403
        return f(*a, **k)
    return w

# ── Шаблони ──────────────────────────────────────────────────
BASE_CSS = """
:root{--ink:#101820;--ink2:#3d4852;--mut:#3d4c5c;--paper:#f2f4f6;--card:#fff;
--line:#c3cfe0;--acc:#152331;--acc-soft:#e3ecec;--gold:#b07a1e;--ok:#1d7a4f;--err:#b3261e;
--grad-light:linear-gradient(135deg,#acb6e5,#86fde8);--grad-dark:linear-gradient(135deg,#152331,#000);
--mono:ui-monospace,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--grad-light);background-attachment:fixed;color:var(--ink);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
input,select,textarea{font:inherit;border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%}
input:focus,select:focus,textarea:focus{outline:2px solid var(--acc)}
button{font:inherit;cursor:pointer;border:none;border-radius:8px;padding:8px 14px}
.btn-p{background:var(--grad-dark);color:#fff;font-weight:600}.btn-p:disabled{background:#9daed9}
.btn-s{background:#fff;border:1px solid var(--line);color:var(--ink2)}
.btn-d{background:#fff;border:1px solid #eacdcb;color:var(--err)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:14px}
.lbl{display:block;font-size:12px;font-weight:600;color:var(--ink2);margin:10px 0 4px}
.hint{font-size:12px;color:var(--mut)}.mono{font-family:var(--mono)}
.chip{font-family:var(--mono);font-size:12px;background:var(--acc-soft);color:#1c5250;border-radius:6px;padding:2px 8px}
.tag-a{background:var(--grad-dark);color:#86fde8}.tag-j{background:var(--acc-soft);color:#1c5250}
table{width:100%;border-collapse:collapse}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);text-align:left;
padding:8px 10px;border-bottom:2px solid var(--line);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
.bar{height:5px;background:var(--acc-soft);border-radius:3px;min-width:70px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--grad-dark)}
.pbar{height:8px;background:var(--acc-soft);border-radius:4px;overflow:hidden;width:100%}
.pbar i{display:block;height:100%;background:var(--grad-dark);transition:width .3s}
a.wl{color:#1c5250;text-decoration:none;font-weight:600}
header{background:var(--grad-dark);color:#fff;padding:12px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header a{color:#cfd8e0;text-decoration:none}header a.on,header a:hover{color:#fff}
.mode{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}
.mode button{border-radius:0;background:#fff;color:var(--ink2);border-right:1px solid var(--line)}
.mode button:last-child{border-right:none}.mode button.on{background:var(--grad-dark);color:#fff;font-weight:600}
main{max-width:1200px;margin:0 auto;padding:20px}
.langsw a{font-size:12px;font-weight:700;padding:4px 8px;border:1px solid #33424f;border-radius:6px;margin-left:4px}
.langsw a.on{background:#86fde8;color:#101820}
.carousel-wrap{position:relative;margin:4px 0}
.carousel{display:flex;gap:12px;overflow-x:auto;padding:4px 2px 12px;scroll-snap-type:x proximity;scrollbar-width:thin}
.carousel::-webkit-scrollbar{height:6px}
.carousel::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}
.art-card{flex:0 0 150px;scroll-snap-align:start;cursor:pointer;border-radius:12px;overflow:hidden;
background:#fff;border:1px solid var(--line);transition:transform .15s,box-shadow .15s}
.art-card:hover{transform:translateY(-3px);box-shadow:0 6px 14px rgba(21,35,49,.18)}
.art-card .thumb{width:100%;height:96px;object-fit:cover;display:block;background:var(--acc-soft)}
.art-card .thumb-ph{width:100%;height:96px;background:var(--acc-soft);display:flex;align-items:center;
justify-content:center;color:var(--mut);font-size:11px}
.art-card .body{padding:8px}
.art-title{font-size:12px;font-weight:600;line-height:1.3;max-height:2.6em;overflow:hidden;color:var(--ink)}
.car-nav{position:absolute;top:34px;width:30px;height:30px;border-radius:50%;background:var(--grad-dark);
color:#fff;display:flex;align-items:center;justify-content:center;z-index:5;
box-shadow:0 2px 6px rgba(0,0,0,.35);font-size:16px;line-height:1}
.car-nav.prev{left:-8px}.car-nav.next{right:-8px}
"""

LOGIN_HTML = """<!doctype html><html lang="{{l}}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{t.brand}}</title><style>""" + BASE_CSS + """</style>
<body style="display:grid;place-items:center;min-height:100vh">
<div class="card" style="width:380px;max-width:95vw">
 <div style="display:flex;justify-content:space-between">
  <div><div class="mono" style="font-size:12px;color:var(--gold);letter-spacing:.12em">WIKI·CONTEST</div>
  <h1 style="margin:4px 0">{{t.brand}}</h1></div>
  <div class="langsw"><a href="?lang=uk" class="{{'on' if l=='uk' else ''}}" style="color:var(--ink2)">УКР</a><a
   href="?lang=en" class="{{'on' if l=='en' else ''}}" style="color:var(--ink2)">ENG</a><a
   href="?lang=pl" class="{{'on' if l=='pl' else ''}}" style="color:var(--ink2)">PL</a></div>
 </div>
 <div class="hint" style="margin-bottom:12px">{{t.tagline}}</div>
 <form method="post">
  <label class="lbl">{{t.login}}</label><input name="login" autofocus>
  <label class="lbl">{{t.password}}</label><input name="password" type="password">
  {% if err %}<div style="color:var(--err);margin-top:8px">{{t.bad}}</div>{% endif %}
  <button class="btn-p" style="width:100%;margin-top:14px">{{t.signin}}</button>
 </form>
 {% if oauth_unknown %}
 <div style="color:var(--err);margin-top:12px;font-size:13px">{{t.oauth_unknown}} «{{oauth_unknown}}»</div>
 {% endif %}
 {% if oauth_enabled %}
 <div style="display:flex;align-items:center;gap:10px;margin:16px 0">
  <div style="flex:1;height:1px;background:var(--line)"></div><span class="hint" style="font-size:11px">{{t.or_}}</span><div style="flex:1;height:1px;background:var(--line)"></div>
 </div>
 <a href="{{ url_for('login_oauth') }}" class="btn-s" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box">{{t.oauth_btn}}</a>
 {% endif %}
</div></body></html>"""

MAIN_HTML = """<!doctype html><html lang="{{l}}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{t.brand}}</title><style>""" + BASE_CSS + """</style>
<body>
<header>
 <div><span class="mono" style="font-size:11px;color:#f5d789;letter-spacing:.12em">WIKI·CONTEST</span>
 <div style="font-weight:800;font-size:18px">{{t.brand}}</div></div>
 {% if contests %}
 <form method="get" style="margin:0"><input type="hidden" name="view" value="{{view}}">
  <select name="contest" onchange="this.form.submit()" style="width:auto;background:#1d2a36;color:#fff;border-color:#33424f">
  {% for c in contests %}<option value="{{c.id}}" {{'selected' if c.id==cid else ''}}>{{c.name}}</option>{% endfor %}
  </select></form>
 {% endif %}
 <nav>
  <a href="?view=analysis&contest={{cid or ''}}" class="{{'on' if view=='analysis' else ''}}">{{t.nav_a}}</a>
  {% if me.role=='admin' %} · <a href="?view=admin&contest={{cid or ''}}" class="{{'on' if view=='admin' else ''}}">{{t.nav_s}}</a>{% endif %}
 </nav>
 <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
  <span class="langsw"><a href="?lang=uk&view={{view}}&contest={{cid or ''}}" class="{{'on' if l=='uk' else ''}}">УКР</a><a
    href="?lang=en&view={{view}}&contest={{cid or ''}}" class="{{'on' if l=='en' else ''}}">ENG</a><a
    href="?lang=pl&view={{view}}&contest={{cid or ''}}" class="{{'on' if l=='pl' else ''}}">PL</a></span>
  <span class="chip {{'tag-a' if me.role=='admin' else 'tag-j'}}">{{t[me.role]}}</span>
  <b>{{me.name}}</b> <a href="{{url_for('logout')}}">{{t.logout}}</a>
 </div>
</header>
<main>
{% if view=='admin' and me.role=='admin' %}{% include 'ADMIN' %}{% else %}{% include 'ANALYSIS' %}{% endif %}
<div class="hint" style="margin-top:14px">{{t.footer}}</div>
</main>
<script>
const T={{tjson|safe}}, CID={{cid|tojson}}, LANG={{l|tojson}};
function pbar(el,done,total,label){
 const pct=total?Math.round(done/total*100):0;
 el.innerHTML=`<div class="pbar"><i style="width:${pct}%"></i></div>
 <div class="hint mono" style="font-size:11px">${done} / ${total} · ${pct}% ${label?'· '+esc(label):''}</div>`;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// анонімний редактор: IPv4, IPv6 або замаскований тимчасовий обліковий запис (~2024-1…)
function isAnon(u){
 if(/^~/.test(u))return true;
 if(/^\\d{1,3}(\\.\\d{1,3}){3}$/.test(u))return true;
 if(/^[0-9a-f]{1,4}(::?[0-9a-f]{0,4}){2,7}$/i.test(u))return true;
 return false;
}
async function mw(api,params){
 const p=new URLSearchParams({format:'json',origin:'*',...params});
 const r=await fetch(api+'?'+p);if(!r.ok)throw new Error('HTTP '+r.status);
 const d=await r.json();if(d.error)throw new Error(d.error.info||'API error');return d;
}
async function fetchContribs(api,user,startISO,endISO){
 let cont=null,all=[],g=0;
 do{const params={action:'query',list:'usercontribs',ucuser:user,uclimit:'500',ucprop:'title|timestamp|sizediff|flags'};
  if(startISO)params.ucend=startISO;if(endISO)params.ucstart=endISO;
  if(cont)params.uccontinue=cont;
  const d=await mw(api,params);
  all.push(...(d.query?.usercontribs||[]));cont=d.continue?.uccontinue||null;g++;
 }while(cont&&g<10);return all;
}
// парсить посилання на шаблон конкурсу -> {api, title, host}
function templateInfo(url){
 try{
  const u=new URL(url.trim());
  const m=u.pathname.match(/\\/wiki\\/(.+)$/);if(!m)return null;
  return{api:u.origin+'/w/api.php',title:decodeURIComponent(m[1]).replace(/_/g,' '),host:u.hostname};
 }catch(e){return null}
}
// офіційні конкурсні статті за шаблоном (з урахуванням "лише обговорення")
async function templatePages(tp,talkOnly){
 let cont=null,raw=[],g=0;
 do{const params={action:'query',list:'embeddedin',eititle:tp.title,eilimit:'500',einamespace:talkOnly?'1':'0|1'};
  if(cont)params.eicontinue=cont;const d=await mw(tp.api,params);
  raw.push(...(d.query?.embeddedin||[]));cont=d.continue?.eicontinue||null;g++;
 }while(cont&&g<10);
 const nsd=await mw(tp.api,{action:'query',meta:'siteinfo',siprop:'namespaces'});
 const talkPfx=(nsd.query?.namespaces?.['1']?.['*']||'Talk')+':';
 const pageSet=new Set();
 raw.forEach(x=>pageSet.add(x.ns===1?x.title.slice(talkPfx.length):x.title));
 return[...pageSet];
}
// повна історія редагувань статті з обчисленим приростом байтів на редагування
async function pageRevisionsWithDiff(api,title){
 let cont=null,all=[],g=0;
 do{
  const params={action:'query',prop:'revisions',titles:title,
   rvprop:'user|timestamp|size|flags',rvlimit:'500',rvdir:'older'};
  if(cont)params.rvcontinue=cont;
  const d=await mw(api,params);
  const page=Object.values(d.query?.pages||{})[0];
  all.push(...(page?.revisions||[]));
  cont=d.continue?.rvcontinue||null;g++;
 }while(cont&&g<10);
 for(let i=0;i<all.length;i++){
  const older=all[i+1];
  all[i].diff=older?(all[i].size-older.size):all[i].size;
 }
 return all;
}
</script>
{% if view=='admin' and me.role=='admin' %}
<script>
async function extractParticipants(){
 const url=document.getElementById('tpl').value.trim();
 const msg=document.getElementById('extmsg'),bar=document.getElementById('extbar');
 const tp=templateInfo(url);if(!tp){msg.textContent='✗ URL';return}
 const s=document.getElementById('start').value,e=document.getElementById('end').value;
 const sISO=s?new Date(s+'T00:00:00Z').toISOString():null,eISO=e?new Date(e+'T23:59:59Z').toISOString():null;
 const btn=document.getElementById('extbtn');btn.disabled=true;msg.textContent=T.tpl_run;
 const talkOnly=document.getElementById('tplTalkOnly').checked;
 try{
  const pages=await templatePages(tp,talkOnly);
  const users=new Set();
  for(let i=0;i<pages.length;i++){
   pbar(bar,i,pages.length,pages[i]);
   const params={action:'query',prop:'revisions',titles:pages[i],rvprop:'user|timestamp',rvlimit:'500'};
   if(sISO)params.rvend=sISO;if(eISO)params.rvstart=eISO;
   const d=await mw(tp.api,params);
   const page=Object.values(d.query?.pages||{})[0];
   (page?.revisions||[]).forEach(r=>{if(r.user&&!/bot$|бот$/i.test(r.user.trim())&&!isAnon(r.user.trim()))users.add(r.user)});
  }
  pbar(bar,pages.length,pages.length,'');
  const ta=document.getElementById('parts');
  const existing=ta.value.split('\\n').map(s=>s.trim()).filter(Boolean);
  ta.value=[...new Set([...existing,...users])].sort((a,b)=>a.localeCompare(b)).join('\\n');
  msg.textContent='✓ '+pages.length+' / '+users.size;
 }catch(err){msg.textContent='✗ '+err.message}
 btn.disabled=false;
}
</script>
{% else %}
<script>
const CONTEST={{cjson|safe}};
let RESULTS={{rjson|safe}}, ASSESS={{ajson|safe}};
const JURY_LOGIN={{me.login|tojson}};
let QUANT=true,QUAL=false;
const projects=(CONTEST?CONTEST.projects:[]).map(p=>{try{const u=new URL(p);
 return{origin:u.origin,api:u.origin+'/w/api.php',label:u.hostname.replace('.org',''),host:u.hostname}}catch(e){return null}}).filter(Boolean);

function setMode(m){if(m==='qual'){QUAL=!QUAL;if(QUAL)QUANT=true}else{if(!QUAL)QUANT=!QUANT}
 document.getElementById('mq').className=QUANT?'on':'';document.getElementById('ml').className=QUAL?'on':'';
 document.getElementById('modehint').textContent=QUAL?T.qual_h:T.modes_h;render()}

async function runCheck(){
 if(!CONTEST)return;
 const btn=document.getElementById('runbtn'),bar=document.getElementById('runbar');
 btn.disabled=true;btn.textContent=T.checking;
 const sISO=CONTEST.start?new Date(CONTEST.start+'T00:00:00Z').toISOString():null;
 const eISO=CONTEST.end?new Date(CONTEST.end+'T23:59:59Z').toISOString():null;
 const out={};
 CONTEST.participants.forEach(u=>out[u]={perProject:{},bytes:0,edits:0,articles:0,_titles:new Set()});

 // якщо в конкурсі вказано шаблон — рахуємо внесок ЛИШЕ в межах статей,
 // які дійсно позначені цим шаблоном (а не весь внесок учасника по вікі)
 const tp=CONTEST.template?templateInfo(CONTEST.template):null;
 let tplHost=null,tplPages=[];
 if(tp){
  try{tplPages=await templatePages(tp,!!CONTEST.tpl_talk_only);tplHost=tp.host}
  catch(e){tplPages=[];tplHost=null}
 }

 const totalSteps=projects.reduce((s,pr)=>s+(pr.host===tplHost?tplPages.length:CONTEST.participants.length),0)||1;
 let done=0;

 for(const pr of projects){
  if(pr.host===tplHost){
   for(let i=0;i<tplPages.length;i++){
    pbar(bar,done,totalSteps,tplPages[i]);done++;
    try{
     const revs=await pageRevisionsWithDiff(pr.api,tplPages[i]);
     revs.forEach(r=>{
      if(!r.timestamp)return;
      if(sISO&&r.timestamp<sISO)return;
      if(eISO&&r.timestamp>eISO)return;
      if(!r.user||!out[r.user])return; // рахуємо тільки офіційних учасників конкурсу
      const row=out[r.user];
      if(!row.perProject[pr.host])row.perProject[pr.host]={bytes:0,edits:0,_titles:new Set(),_detail:{}};
      const pp=row.perProject[pr.host];
      if(r.diff>0){pp.bytes+=r.diff;row.bytes+=r.diff}
      pp.edits++;row.edits++;
      pp._titles.add(tplPages[i]);row._titles.add(pr.host+'::'+tplPages[i]);
      if(!pp._detail[tplPages[i]])pp._detail[tplPages[i]]={bytes:0,created:false};
      if(r.diff>0)pp._detail[tplPages[i]].bytes+=r.diff;
      if('new' in r)pp._detail[tplPages[i]].created=true;
     });
    }catch(e){/* сторінку могли видалити/перейменувати — пропускаємо */}
   }
  }else{
   for(let i=0;i<CONTEST.participants.length;i++){
    const user=CONTEST.participants[i];pbar(bar,done,totalSteps,user);done++;
    const row=out[user];
    try{const cs=await fetchContribs(pr.api,user,sISO,eISO);let b=0;
     const detail={};
     cs.forEach(c=>{
      if(c.sizediff>0)b+=c.sizediff;
      row._titles.add(pr.host+'::'+c.title);
      if(!detail[c.title])detail[c.title]={bytes:0,created:false};
      if(c.sizediff>0)detail[c.title].bytes+=c.sizediff;
      if('new' in c)detail[c.title].created=true;
     });
     const list=Object.entries(detail).map(([title,d])=>({title,bytes:d.bytes,created:d.created}))
      .sort((a,b2)=>b2.bytes-a.bytes);
     row.perProject[pr.host]={bytes:b,edits:cs.length,articles:new Set(cs.map(c=>c.title)).size,list};
     row.bytes+=b;row.edits+=cs.length;
    }catch(e){row.perProject[pr.host]={err:String(e.message||e)}}
   }
  }
 }
 Object.values(out).forEach(row=>{
  const pp=row.perProject[tplHost];
  if(pp&&pp._detail){
   pp.list=Object.entries(pp._detail).map(([title,d])=>({title,bytes:d.bytes,created:d.created}))
    .sort((a,b2)=>b2.bytes-a.bytes);
   pp.articles=pp._titles.size;delete pp._detail;delete pp._titles;
  }
  row.articles=row._titles.size;delete row._titles;
 });
 pbar(bar,totalSteps,totalSteps,'');
 RESULTS=out;
 await fetch('/api/results/'+CID,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});
 btn.disabled=false;btn.textContent=T.check;render();
}
async function saveScore(user,article,val){
 await fetch('/api/score',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({contest:CID,participant:user,article,value:val===''?null:Number(val)})});
 const r=await fetch('/api/state/'+CID);ASSESS=(await r.json()).assess;render();
}
async function saveComment(user,article){
 const inp=document.getElementById('cm_'+idFor(user+'||'+article));
 const text=inp.value.trim();if(!text)return;
 await fetch('/api/comment',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({contest:CID,participant:user,article,text})});
 const r=await fetch('/api/state/'+CID);ASSESS=(await r.json()).assess;render();
}
let ARTOPEN=null;
function articleLink(origin,title){return origin+'/wiki/'+encodeURIComponent(title.replace(/ /g,'_'))}

// ── Модальне прев'ю статті: превʼю зліва, оцінка/коментарі справа ──
let MODAL=null; // {user,title,origin,host,api,label,bytes,created}
const EXTRACT_CACHE={};
const THUMB_PENDING=new Set();
// пакетне попереднє завантаження мініатюр для карусельних карток
async function prefetchThumbs(api,host,titles){
 const need=titles.filter(t=>!EXTRACT_CACHE[host+'::'+t]);
 if(!need.length||THUMB_PENDING.has(host))return;
 THUMB_PENDING.add(host);
 for(let i=0;i<need.length;i+=50){
  const chunk=need.slice(i,i+50);
  try{
   const d=await mw(api,{action:'query',prop:'pageimages',piprop:'thumbnail',pithumbsize:'300',titles:chunk.join('|')});
   Object.values(d.query?.pages||{}).forEach(p=>{
    EXTRACT_CACHE[host+'::'+p.title]={thumb:p.thumbnail?p.thumbnail.source:null,extract:null};
   });
  }catch(e){/* ігноруємо помилки мініатюр — картки покажуться без зображення */}
  chunk.forEach(t=>{const key=host+'::'+t;if(!EXTRACT_CACHE[key])EXTRACT_CACHE[key]={thumb:null,extract:null}});
  render();
 }
 THUMB_PENDING.delete(host);
}
async function openArticle(user,title,origin,host,api,label,bytes,created){
 MODAL={user,title,origin,host,api,label,bytes,created};
 renderModal();
 const key=host+'::'+title;
 const cached=EXTRACT_CACHE[key];
 if(!cached||cached.extract==null){
  try{
   const d=await mw(api,{action:'query',prop:'extracts|pageimages',exintro:'1',explaintext:'1',
    exchars:'700',piprop:'thumbnail',pithumbsize:'500',redirects:'1',titles:title});
   const page=Object.values(d.query?.pages||{})[0];
   EXTRACT_CACHE[key]={extract:page?.extract||'',thumb:page?.thumbnail?.source||(cached&&cached.thumb)||null};
  }catch(e){EXTRACT_CACHE[key]={extract:'',thumb:(cached&&cached.thumb)||null,err:String(e.message||e)}}
  if(MODAL&&MODAL.title===title&&MODAL.host===host)renderModal();
 }
}
function closeModal(){MODAL=null;renderModal()}
function renderModal(){
 let el=document.getElementById('artModal');
 if(!MODAL){if(el)el.remove();return}
 if(!el){el=document.createElement('div');el.id='artModal';document.body.appendChild(el)}
 const key=MODAL.host+'::'+MODAL.title;
 const ex=EXTRACT_CACHE[key];
 const ad=(ASSESS[MODAL.user]&&ASSESS[MODAL.user][MODAL.title])||{scores:{},comments:[]};
 const vals=Object.values(ad.scores||{}).filter(v=>v!=null);
 const avg=vals.length?(vals.reduce((x,y)=>x+y,0)/vals.length):null;
 const my=(ad.scores||{})[JURY_LOGIN];
 const cid=idFor(MODAL.user+'||'+MODAL.title);
 let h='<div style="position:fixed;inset:0;background:rgba(16,24,32,.6);z-index:1000;display:flex;align-items:center;justify-content:center;padding:16px" onclick="if(event.target===this)closeModal()">';
 h+='<div style="background:#fff;border-radius:14px;max-width:920px;width:100%;max-height:92vh;overflow:auto;display:grid;grid-template-columns:1fr 1fr" onclick="event.stopPropagation()">';
 h+='<div style="padding:22px;border-right:1px solid var(--line)">';
 if(!ex||ex.extract==null)h+='<div class="hint">'+T.loading+'</div>';
 else if(ex.err)h+='<div class="hint">'+esc(ex.err)+'</div>';
 else{
  if(ex.thumb)h+='<img src="'+esc(ex.thumb)+'" alt="" style="max-width:100%;border-radius:8px;margin-bottom:10px;display:block">';
  h+='<h3 style="margin:0 0 8px">'+esc(MODAL.title)+'</h3>';
  h+='<div style="font-size:14px;line-height:1.55;white-space:pre-wrap">'+esc(ex.extract||'')+'</div>';
 }
 h+='<a class="btn-p" style="display:inline-block;margin-top:14px;text-decoration:none" target="_blank" rel="noopener" href="'+articleLink(MODAL.origin,MODAL.title)+'">'+T.open_article+' ↗</a>';
 h+='</div>';
 h+='<div style="padding:22px">';
 h+='<button class="btn-s" style="float:right;padding:4px 10px" onclick="closeModal()">✕</button>';
 h+='<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
 h+='<span class="chip" style="background:'+(MODAL.created?'#e6f4ea':'var(--acc-soft)')+';color:'+(MODAL.created?'#1d7a4f':'var(--acc)')+'">'+(MODAL.created?T.created:T.expanded)+'</span>';
 h+='<span class="hint mono">'+esc(MODAL.label)+' · +'+fmtN(MODAL.bytes)+(LANG==='uk'?' Б':' B')+'</span>';
 h+='</div>';
 h+='<div class="hint" style="margin:8px 0 4px">'+esc(MODAL.user)+'</div>';
 h+='<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px">';
 h+='<label class="hint" style="font-size:12px">'+T.th_my+':</label>';
 h+='<input type="number" step="0.1" style="width:70px" value="'+(my??'')+'" onchange="saveScore('+esc(JSON.stringify(MODAL.user))+','+esc(JSON.stringify(MODAL.title))+',this.value)">';
 h+='<span class="hint mono">'+T.th_avg+': '+(avg!=null?avg.toFixed(2):'—')+(vals.length?' ('+vals.length+' '+T.scores_n+')':'')+'</span>';
 h+='</div>';
 h+='<div style="margin-top:14px">';
 (ad.comments||[]).forEach(c=>{h+='<div style="padding:5px 0;border-bottom:1px dashed var(--line)"><b>'+esc(c.jury)+'</b> <span class="hint mono">'+esc(c.date)+'</span><div>'+esc(c.text)+'</div></div>'});
 if(!(ad.comments||[]).length)h+='<div class="hint" style="font-size:12px">'+T.no_comments+'</div>';
 h+='<div style="display:flex;gap:8px;margin-top:8px"><input id="cm_'+cid+'" placeholder="'+T.comment_ph+'" onkeydown="if(event.key===\\'Enter\\')saveComment('+esc(JSON.stringify(MODAL.user))+','+esc(JSON.stringify(MODAL.title))+')"><button class="btn-p" onclick="saveComment('+esc(JSON.stringify(MODAL.user))+','+esc(JSON.stringify(MODAL.title))+')">'+T.save+'</button></div>';
 h+='</div>';
 h+='</div></div></div>';
 el.innerHTML=h;
}
function articleQuality(pa){
 if(!pa)return{quality:0,n:0};
 const avgs=[];let n=0;
 Object.values(pa).forEach(a=>{
  const vals=Object.values(a.scores||{}).filter(v=>v!=null);
  if(vals.length){avgs.push(vals.reduce((x,y)=>x+y,0)/vals.length);n+=vals.length}
 });
 return{quality:avgs.length?avgs.reduce((x,y)=>x+y,0)/avgs.length:0,n};
}
function evalFormula(f,v){
 const cleaned=f.replace(/\\b(bytes|edits|articles|quality)\\b/g,'1');
 if(!/^[\\d\\s+\\-*/().,]+$/.test(cleaned))return null;
 try{const fn=new Function('bytes','edits','articles','quality','return ('+f+')');
  const r=fn(v.bytes,v.edits,v.articles,v.quality);
  return typeof r==='number'&&!isNaN(r)?r:null}catch(e){return null}
}
function fmtN(n){return n==null?'—':n.toLocaleString(LANG==='uk'?'uk-UA':'en-GB')}
function idFor(u){return btoa(unescape(encodeURIComponent(u))).replace(/[^a-z0-9]/gi,'')}
function contribLink(origin,user){return origin+'/wiki/Special:Contributions/'+encodeURIComponent(user)}

function render(){
 const el=document.getElementById('tbl');if(!CONTEST){el.innerHTML='';return}
 const rows=CONTEST.participants.map(user=>{
  const r=RESULTS?RESULTS[user]:null;const pa=ASSESS[user]||{};
  const{quality,n}=articleQuality(pa);
  const score=r?evalFormula(CONTEST.formula,{bytes:r.bytes,edits:r.edits,articles:r.articles,quality}):null;
  return{user,r,pa,quality,n,score};
 }).sort((x,y)=>QUAL?((y.score??-1e18)-(x.score??-1e18)):((y.r?y.r.bytes:-1)-(x.r?x.r.bytes:-1)));
 const maxB=Math.max(1,...rows.map(r=>r.r?r.r.bytes:0));
 let h='<table><thead><tr><th>#</th><th>'+T.th_u+'</th>';
 if(QUANT){h+='<th>'+T.th_b+'</th>';projects.forEach(p=>h+='<th>'+esc(p.label)+'</th>');h+='<th>'+T.th_e+'</th>'}
 if(QUAL)h+='<th>'+T.th_avg+'</th><th>'+T.th_score+'</th>';
 h+='<th></th></tr></thead><tbody>';
 if(!rows.length)h+='<tr><td colspan="99" class="hint" style="padding:20px">'+T.no_parts+'</td></tr>';
 rows.forEach((row,i)=>{
  h+='<tr><td class="mono" style="color:var(--mut)">'+(i+1)+'</td><td>';
  h+='<a class="wl" target="_blank" rel="noopener" href="'+contribLink(projects[0]?projects[0].origin:'',row.user)+'">'+esc(row.user)+' ↗</a>';
  h+='<div style="display:flex;gap:8px">'+projects.map(p=>'<a class="hint" target="_blank" rel="noopener" href="'+contribLink(p.origin,row.user)+'">'+esc(p.label)+' ↗</a>').join('')+'</div></td>';
  if(QUANT){
   h+='<td><div class="mono" style="font-weight:700">'+(row.r?'+'+fmtN(row.r.bytes)+(LANG==='uk'?' Б':' B'):'—')+'</div>';
   h+='<div class="bar"><i style="width:'+((row.r?row.r.bytes:0)/maxB*100)+'%"></i></div></td>';
   projects.forEach(p=>{const pp=row.r?row.r.perProject[p.host]:null;
    h+='<td class="mono" style="font-size:12px">'+(pp?(pp.err?'<span style="color:var(--err)" title="'+esc(pp.err)+'">'+T.err+'</span>':'+'+fmtN(pp.bytes)):'—')+'</td>'});
   h+='<td class="mono" style="font-size:12px">'+(row.r?fmtN(row.r.edits)+' / '+fmtN(row.r.articles):'—')+'</td>';
  }
  if(QUAL){
   h+='<td class="mono">'+(row.n?row.quality.toFixed(2):'—')+'<div class="hint">'+row.n+' '+T.scores_n+'</div></td>';
   h+='<td><span class="mono" style="font-weight:700;color:var(--gold);font-size:15px">'+(row.score!=null?row.score.toFixed(2):'—')+'</span></td>';
  }
  h+='<td>'+(QUAL?'<button class="btn-s" style="padding:4px 10px;font-size:12px" onclick="ARTOPEN=ARTOPEN==='+esc(JSON.stringify(row.user))+'?null:'+esc(JSON.stringify(row.user))+';render()">'+T.articles_btn+(row.r?' ('+row.r.articles+')':'')+'</button>':'')+'</td></tr>';
  if(QUAL&&ARTOPEN===row.user){
   h+='<tr><td colspan="99" style="background:#fafbfc"><div style="max-width:900px">';
   const items=[];
   projects.forEach(p=>{const pp=row.r?row.r.perProject[p.host]:null;
    (pp&&pp.list?pp.list:[]).forEach(it=>items.push({...it,origin:p.origin,label:p.label,host:p.host,api:p.api}))});
   items.sort((a,b2)=>b2.bytes-a.bytes);
   if(!items.length)h+='<div class="hint">'+T.no_articles+'</div>';
   else{
    if(items[0])prefetchThumbs(items[0].api,items[0].host,items.map(i=>i.title));
    h+='<div class="carousel-wrap">';
    h+='<button class="car-nav prev" onclick="this.nextElementSibling.scrollBy({left:-320,behavior:\\'smooth\\'})">‹</button>';
    h+='<div class="carousel">';
    items.forEach(it=>{
     const ad=(row.pa&&row.pa[it.title])||{scores:{},comments:[]};
     const vals=Object.values(ad.scores||{}).filter(v=>v!=null);
     const avg=vals.length?(vals.reduce((x,y)=>x+y,0)/vals.length):null;
     const nCm=(ad.comments||[]).length;
     const ex=EXTRACT_CACHE[it.host+'::'+it.title];
     const thumbHtml=ex&&ex.thumb?'<img class="thumb" src="'+esc(ex.thumb)+'" alt="">':'<div class="thumb-ph">'+(ex?'—':'…')+'</div>';
     h+='<div class="art-card" onclick="openArticle('+esc(JSON.stringify(row.user))+','+esc(JSON.stringify(it.title))+','+esc(JSON.stringify(it.origin))+','+esc(JSON.stringify(it.host))+','+esc(JSON.stringify(it.api))+','+esc(JSON.stringify(it.label))+','+it.bytes+','+(it.created?'true':'false')+')">';
     h+=thumbHtml;
     h+='<div class="body"><div class="art-title">'+esc(it.title)+'</div>';
     h+='<div class="hint" style="font-size:10px;margin-top:4px">'+it.label+' · +'+fmtN(it.bytes)+(LANG==='uk'?' Б':' B')+'</div>';
     h+='<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:5px">';
     h+='<span class="chip" style="font-size:10px;background:'+(it.created?'#e6f4ea':'var(--acc-soft)')+';color:'+(it.created?'#1d7a4f':'var(--acc)')+'">'+(it.created?T.created:T.expanded)+'</span>';
     if(avg!=null)h+='<span class="chip" style="font-size:10px;background:#fbf3e2;color:#b07a1e">★ '+avg.toFixed(1)+'</span>';
     if(nCm)h+='<span class="hint" style="font-size:10px">💬'+nCm+'</span>';
     h+='</div></div></div>';
    });
    h+='</div>';
    h+='<button class="car-nav next" onclick="this.previousElementSibling.scrollBy({left:320,behavior:\\'smooth\\'})">›</button>';
    h+='</div>';
   }
   h+='</div></td></tr>';
  }
 });
 h+='</tbody></table>';
 el.innerHTML=h;
 document.getElementById('csvlink').href='/export/'+CID+'.csv?quant='+(QUANT?1:0)+'&qual='+(QUAL?1:0);
 renderModal();
}
window.addEventListener('DOMContentLoaded',render);
</script>
{% endif %}
</body></html>"""

ADMIN_TPL = """
<div class="mode" style="margin-bottom:14px">
 <button class="on" onclick="document.getElementById('sec-c').style.display='block';document.getElementById('sec-u').style.display='none';document.getElementById('sec-p').style.display='none';[...this.parentNode.children].forEach(b=>b.className='');this.className='on'">{{t.tab_c}}</button>
 <button onclick="document.getElementById('sec-c').style.display='none';document.getElementById('sec-u').style.display='block';document.getElementById('sec-p').style.display='none';[...this.parentNode.children].forEach(b=>b.className='');this.className='on'">{{t.tab_u}}</button>
 <button onclick="document.getElementById('sec-c').style.display='none';document.getElementById('sec-u').style.display='none';document.getElementById('sec-p').style.display='block';[...this.parentNode.children].forEach(b=>b.className='');this.className='on'">{{t.tab_p}}</button>
</div>

<div id="sec-c" style="display:grid;gap:16px;grid-template-columns:minmax(320px,1fr) minmax(280px,360px)">
 <div class="card">
  <h3 style="margin-top:0">{{t.edit_c if edit_contest else t.new_c}}</h3>
  <form method="post" action="{{url_for('contest_save')}}">
   <input type="hidden" name="id" value="{{edit_contest.id if edit_contest else ''}}">
   <label class="lbl">{{t.c_name}}</label>
   <input name="name" value="{{edit_contest.name if edit_contest else ''}}" placeholder="Cultural Heritage 2026" required>
   <label class="lbl">{{t.projects}}</label>
   <textarea name="projects" rows="3" class="mono" style="font-size:12px">{{'\n'.join(edit_contest.projects) if edit_contest else 'https://uk.wikivoyage.org/wiki/Головна_сторінка\nhttps://uk.wikiquote.org/wiki/Головна_сторінка'}}</textarea>
   <div class="hint">{{t.projects_h}}</div>
   <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
    <div><label class="lbl">{{t.d_start}}</label><input type="date" id="start" name="start" value="{{edit_contest.start if edit_contest else ''}}"></div>
    <div><label class="lbl">{{t.d_end}}</label><input type="date" id="end" name="end" value="{{edit_contest.end if edit_contest else ''}}"></div>
   </div>
   <div style="margin-top:12px;background:var(--acc-soft);border-radius:10px;padding:10px 12px">
    <label class="lbl" style="margin-top:0">{{t.tpl}}</label>
    <input id="tpl" name="template" class="mono" style="font-size:12px"
     value="{{edit_contest.template if edit_contest else ''}}"
     placeholder="https://uk.wikivoyage.org/wiki/Шаблон:Cultural_Heritage_and_Notable_Personalities_2026">
    <label class="hint" style="display:flex;align-items:center;gap:6px;margin-top:8px;cursor:pointer">
     <input type="checkbox" id="tplTalkOnly" name="tpl_talk_only" style="width:auto"
      {{'checked' if edit_contest and edit_contest.tpl_talk_only else ''}}> {{t.tpl_talk_only}}
    </label>
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center">
     <button type="button" id="extbtn" class="btn-p" onclick="extractParticipants()">{{t.tpl_btn}}</button>
     <span id="extmsg" class="hint mono"></span>
    </div>
    <div id="extbar" style="margin-top:6px"></div>
    <div class="hint" style="margin-top:6px">{{t.tpl_h}}</div>
   </div>
   <label class="lbl">{{t.parts}}</label>
   <textarea id="parts" name="participants" rows="6">{{'\n'.join(edit_contest.participants) if edit_contest else ''}}</textarea>
   <label class="lbl">{{t.formula}}</label>
   <input name="formula" class="mono" value="{{edit_contest.formula if edit_contest else 'bytes / 1000 + quality * 10'}}">
   <div class="hint">{{t.formula_h}}</div>
   {% if msg %}<div style="color:var(--ok);font-weight:600;margin-top:8px">{{msg}}</div>{% endif %}
   <div style="display:flex;gap:8px;margin-top:14px">
    <button class="btn-p">{{t.save if edit_contest else t.create}}</button>
    {% if edit_contest %}<a href="?view=admin" class="btn-s" style="text-decoration:none;padding:8px 14px">{{t.cancel}}</a>{% endif %}
   </div>
  </form>
 </div>
 <div class="card">
  <h3 style="margin-top:0">{{t.tab_c}} ({{contests|length}})</h3>
  {% if not contests %}<div class="hint">{{t.no_c}}</div>{% endif %}
  {% for c in contests %}
  <div style="border-bottom:1px solid var(--line);padding:10px 0">
   <div style="font-weight:700">{{c.name}}</div>
   <div class="hint">{{c.hosts}} · {{t.parts_n}}: {{c.participants|length}}</div>
   <div class="mono hint">{{c.formula}}</div>
   <div style="display:flex;gap:6px;margin-top:4px">
    <a class="btn-s" style="padding:4px 10px;font-size:12px;text-decoration:none" href="?view=admin&edit={{c.id}}">{{t.edit}}</a>
    <form method="post" action="{{url_for('contest_delete')}}" style="margin:0">
     <input type="hidden" name="id" value="{{c.id}}">
     <button class="btn-d" style="padding:4px 10px;font-size:12px">{{t.delete}}</button></form>
   </div>
  </div>
  {% endfor %}
 </div>
</div>

<div id="sec-u" style="display:none">
 <div style="display:grid;gap:16px;grid-template-columns:minmax(300px,420px) 1fr">
  <div class="card"><h3 style="margin-top:0">{{t.add_u}}</h3>
   <form method="post" action="{{url_for('user_save')}}">
    <label class="lbl">{{t.role}}</label>
    <select name="role"><option value="jury">{{t.jury}}</option><option value="admin">{{t.admin}}</option></select>
    <label class="lbl">{{t.u_name}}</label><input name="name">
    <label class="lbl">{{t.login}}</label><input name="login" required>
    <label class="lbl">{{t.password}}</label><input name="password" type="password" required>
    <button class="btn-p" style="margin-top:12px">{{t.create}}</button>
   </form></div>
  <div class="card"><h3 style="margin-top:0">{{t.accounts}} ({{users|length}})</h3>
   {% for u in users %}
   <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
    <span class="chip {{'tag-a' if u.role=='admin' else 'tag-j'}}">{{t[u.role]}}</span>
    <b>{{u.name}}</b><span class="mono hint">{{u.login}}</span>
    {% if u.login!=me.login %}
    <form method="post" action="{{url_for('user_delete')}}" style="margin-left:auto">
     <input type="hidden" name="login" value="{{u.login}}">
     <button class="btn-d" style="padding:3px 10px;font-size:12px">{{t.delete}}</button></form>
    {% else %}<span class="hint" style="margin-left:auto">{{t.its_you}}</span>{% endif %}
   </div>{% endfor %}</div>
 </div>
</div>

<div id="sec-p" style="display:none">
 <div class="card" style="max-width:400px"><h3 style="margin-top:0">{{t.ch_pass}} — {{me.name}}</h3>
  <form method="post" action="{{url_for('change_password')}}">
   <label class="lbl">{{t.new_pass}}</label><input name="p1" type="password" required minlength="4">
   <label class="lbl">{{t.again}}</label><input name="p2" type="password" required minlength="4">
   <button class="btn-p" style="margin-top:12px">{{t.save}}</button>
  </form></div>
</div>
"""

ANALYSIS_TPL = """
{% if not contest %}
 <div class="card hint">{{t.no_c}}</div>
{% else %}
<div class="card" style="display:flex;flex-wrap:wrap;gap:14px;align-items:center">
 <div>
  <div class="lbl" style="margin:0">{{t.mode}}</div>
  <div class="mode" style="margin-top:4px">
   <button id="mq" class="on" onclick="setMode('quant')">{{t.quant}}</button>
   <button id="ml" onclick="setMode('qual')">{{t.qual}}</button>
  </div>
  <div id="modehint" class="hint" style="margin-top:4px">{{t.modes_h}}</div>
 </div>
 <div style="margin-left:auto;text-align:right;min-width:280px">
  <div style="display:flex;gap:8px;justify-content:flex-end">
   <a id="csvlink" class="btn-s" style="text-decoration:none;padding:8px 14px" href="/export/{{cid}}.csv?quant=1&qual=0">{{t.export}}</a>
   <button id="runbtn" class="btn-p" onclick="runCheck()">{{t.check}}</button>
  </div>
  <div id="runbar" style="margin-top:6px"></div>
 </div>
</div>
<div class="card" style="padding:0;overflow-x:auto"><div id="tbl"></div></div>
<div class="hint">{{t.f_of}} <span class="chip">{{contest.formula}}</span></div>
{% endif %}
"""

# ── Допоміжне ────────────────────────────────────────────────
def get_contests():
    rows = db().execute("SELECT * FROM contests").fetchall()
    out = []
    for r in rows:
        c = dict(r)
        c["projects"] = json.loads(c["projects"] or "[]")
        c["participants"] = json.loads(c["participants"] or "[]")
        hosts = []
        for p in c["projects"]:
            m = re.match(r"https?://([^/]+)", p)
            if m:
                hosts.append(m.group(1).replace(".org", ""))
        c["hosts"] = " · ".join(hosts)
        out.append(c)
    return out

def get_assess(cid):
    """Оцінки та коментарі по кожній конкурсній статті окремо.
    Структура: {учасник: {стаття: {"scores":{журі:бал}, "comments":[...]}}}"""
    assess = {}
    for r in db().execute("SELECT * FROM article_scores WHERE contest_id=?", (cid,)):
        p = assess.setdefault(r["participant"], {})
        a = p.setdefault(r["article"], {"scores": {}, "comments": []})
        a["scores"][r["jury"]] = r["value"]
    for r in db().execute(
            "SELECT * FROM article_comments WHERE contest_id=? ORDER BY id", (cid,)):
        p = assess.setdefault(r["participant"], {})
        a = p.setdefault(r["article"], {"scores": {}, "comments": []})
        a["comments"].append({"jury": r["jury_name"], "text": r["text"], "date": r["date"]})
    return assess

def article_quality(participant_assess):
    """Якість учасника = середнє арифметичне середніх оцінок по кожній статті
    (спершу усереднюємо оцінки журі в межах статті, потім — по статтях)."""
    if not participant_assess:
        return 0.0, 0
    article_avgs = []
    n = 0
    for a in participant_assess.values():
        vals = [v for v in (a.get("scores") or {}).values() if v is not None]
        if vals:
            article_avgs.append(sum(vals) / len(vals))
            n += len(vals)
    quality = sum(article_avgs) / len(article_avgs) if article_avgs else 0.0
    return quality, n

class D(dict):
    __getattr__ = dict.get

# ── Маршрути ─────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login_view():
    l = lang()
    err = False
    if request.method == "POST":
        row = db().execute("SELECT * FROM users WHERE login=?",
                           (request.form.get("login", "").strip(),)).fetchone()
        if row and check_password_hash(row["pass"], request.form.get("password", "")):
            session["user"] = {"login": row["login"], "name": row["name"], "role": row["role"]}
            return redirect(url_for("index"))
        err = True
    return render_template_string(LOGIN_HTML, t=D(T[l]), l=l, err=err, oauth_enabled=OAUTH_ENABLED)

@app.route("/login/oauth")
def login_oauth():
    if not OAUTH_ENABLED:
        return redirect(url_for("login_view"))
    consumer_token = mwoauth.ConsumerToken(OAUTH_CONSUMER_KEY, OAUTH_CONSUMER_SECRET)
    try:
        redirect_url, request_token = mwoauth.initiate(OAUTH_MWURI, consumer_token)
    except Exception:
        app.logger.exception("mwoauth.initiate failed")
        return redirect(url_for("login_view"))
    session["oauth_request_token"] = dict(zip(request_token._fields, request_token))
    return redirect(redirect_url)

@app.route("/oauth-callback")
def oauth_callback():
    l = lang()
    if not OAUTH_ENABLED or "oauth_request_token" not in session:
        return redirect(url_for("login_view"))
    consumer_token = mwoauth.ConsumerToken(OAUTH_CONSUMER_KEY, OAUTH_CONSUMER_SECRET)
    request_token = mwoauth.RequestToken(**session.pop("oauth_request_token"))
    try:
        access_token = mwoauth.complete(
            OAUTH_MWURI, consumer_token, request_token, request.query_string)
        identity = mwoauth.identify(OAUTH_MWURI, consumer_token, access_token)
    except Exception:
        app.logger.exception("OAuth authentication failed")
        return render_template_string(LOGIN_HTML, t=D(T[l]), l=l, err=True, oauth_enabled=OAUTH_ENABLED)
    username = identity.get("username", "")
    row = db().execute("SELECT * FROM users WHERE login=?", (username,)).fetchone()
    if not row:
        return render_template_string(LOGIN_HTML, t=D(T[l]), l=l, oauth_enabled=OAUTH_ENABLED,
                                       oauth_unknown=username)
    session["user"] = {"login": row["login"], "name": row["name"], "role": row["role"]}
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login_view"))

@app.route("/")
@login_required
def index():
    l = lang()
    me = D(session["user"])
    contests = [D(c) for c in get_contests()]
    cid = request.args.get("contest") or (contests[0].id if contests else None)
    contest = next((c for c in contests if c.id == cid), None)
    view = request.args.get("view", "analysis")
    if view == "admin" and me.role != "admin":
        view = "analysis"
    edit_contest = None
    if view == "admin" and request.args.get("edit"):
        edit_contest = next((c for c in contests if c.id == request.args["edit"]), None)
    results = None
    assess = {}
    if contest:
        rr = db().execute("SELECT data FROM results WHERE contest_id=?", (contest.id,)).fetchone()
        results = json.loads(rr["data"]) if rr else None
        assess = get_assess(contest.id)
    tpl = MAIN_HTML.replace("{% include 'ADMIN' %}", ADMIN_TPL)\
                   .replace("{% include 'ANALYSIS' %}", ANALYSIS_TPL)
    users = [D(dict(u)) for u in db().execute("SELECT * FROM users").fetchall()]
    return render_template_string(
        tpl, t=D(T[l]), l=l, me=me, contests=contests, cid=cid, contest=contest,
        view=view, edit_contest=edit_contest, users=users,
        msg=request.args.get("msg"),
        tjson=json.dumps(T[l], ensure_ascii=False),
        cjson=json.dumps(contest, ensure_ascii=False) if contest else "null",
        rjson=json.dumps(results, ensure_ascii=False) if results else "null",
        ajson=json.dumps(assess, ensure_ascii=False))

@app.route("/admin/contest/save", methods=["POST"])
@admin_required
def contest_save():
    f = request.form
    projects = [s.strip() for s in f.get("projects", "").splitlines() if s.strip()]
    participants = [s.strip() for s in f.get("participants", "").splitlines() if s.strip()]
    formula = f.get("formula", "").strip()
    if not validate_formula(formula):
        formula = "bytes / 1000 + quality * 10"
    cid = f.get("id") or "c" + hex(int(time.time() * 1000))[2:]
    db().execute("""INSERT INTO contests VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name,template=excluded.template,
        projects=excluded.projects,participants=excluded.participants,
        formula=excluded.formula,start=excluded.start,end=excluded.end,
        tpl_talk_only=excluded.tpl_talk_only""",
        (cid, f.get("name", "").strip(), f.get("template", "").strip(),
         json.dumps(projects), json.dumps(participants), formula,
         f.get("start", ""), f.get("end", ""), 1 if f.get("tpl_talk_only") else 0))
    db().commit()
    return redirect(url_for("index", view="admin", contest=cid, msg=T[lang()]["saved"]))

@app.route("/admin/contest/delete", methods=["POST"])
@admin_required
def contest_delete():
    cid = request.form["id"]
    for tbl in ("contests", "results", "scores", "comments"):
        db().execute(f"DELETE FROM {tbl} WHERE {'id' if tbl=='contests' else 'contest_id'}=?", (cid,))
    db().commit()
    return redirect(url_for("index", view="admin"))

@app.route("/admin/user/save", methods=["POST"])
@admin_required
def user_save():
    f = request.form
    login = f.get("login", "").strip()
    if login and f.get("password"):
        db().execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?)",
                     (login, f.get("name", "").strip() or login,
                      generate_password_hash(f["password"]),
                      "admin" if f.get("role") == "admin" else "jury"))
        db().commit()
    return redirect(url_for("index", view="admin"))

@app.route("/admin/user/delete", methods=["POST"])
@admin_required
def user_delete():
    if request.form["login"] != session["user"]["login"]:
        db().execute("DELETE FROM users WHERE login=?", (request.form["login"],))
        db().commit()
    return redirect(url_for("index", view="admin"))

@app.route("/password", methods=["POST"])
@login_required
def change_password():
    p1, p2 = request.form.get("p1", ""), request.form.get("p2", "")
    if len(p1) >= 4 and p1 == p2:
        db().execute("UPDATE users SET pass=? WHERE login=?",
                     (generate_password_hash(p1), session["user"]["login"]))
        db().commit()
    return redirect(url_for("index", view="admin"))

# ── API для фронтенду ────────────────────────────────────────
@app.route("/api/results/<cid>", methods=["POST"])
@login_required
def api_results(cid):
    db().execute("""INSERT INTO results VALUES(?,?,?)
        ON CONFLICT(contest_id) DO UPDATE SET data=excluded.data,updated=excluded.updated""",
        (cid, json.dumps(request.get_json(), ensure_ascii=False), int(time.time())))
    db().commit()
    return jsonify(ok=True)

@app.route("/api/score", methods=["POST"])
@login_required
def api_score():
    d = request.get_json()
    article = d.get("article", "")
    if d.get("value") is None:
        db().execute("DELETE FROM article_scores WHERE contest_id=? AND participant=? AND article=? AND jury=?",
                     (d["contest"], d["participant"], article, session["user"]["login"]))
    else:
        db().execute("""INSERT INTO article_scores VALUES(?,?,?,?,?)
            ON CONFLICT(contest_id,participant,article,jury) DO UPDATE SET value=excluded.value""",
            (d["contest"], d["participant"], article, session["user"]["login"], float(d["value"])))
    db().commit()
    return jsonify(ok=True)

@app.route("/api/comment", methods=["POST"])
@login_required
def api_comment():
    d = request.get_json()
    db().execute("INSERT INTO article_comments(contest_id,participant,article,jury_name,text,date) VALUES(?,?,?,?,?,?)",
                 (d["contest"], d["participant"], d.get("article", ""), session["user"]["name"],
                  d["text"][:2000], time.strftime("%Y-%m-%d %H:%M")))
    db().commit()
    return jsonify(ok=True)

@app.route("/api/state/<cid>")
@login_required
def api_state(cid):
    return jsonify(assess=get_assess(cid))

# ── Експорт CSV ──────────────────────────────────────────────
@app.route("/export/<cid>.csv")
@login_required
def export_csv(cid):
    l = lang()
    t = T[l]
    quant = request.args.get("quant", "1") == "1"
    qual = request.args.get("qual", "0") == "1"
    contest = next((c for c in get_contests() if c["id"] == cid), None)
    if not contest:
        return "Not found", 404
    rr = db().execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
    results = json.loads(rr["data"]) if rr else {}
    assess = get_assess(cid)
    hosts = []
    for p in contest["projects"]:
        m = re.match(r"https?://([^/]+)", p)
        if m:
            hosts.append(m.group(1))

    rows = []
    for user in contest["participants"]:
        r = results.get(user)
        a = assess.get(user, {})
        quality, n = article_quality(a)
        score = eval_formula(contest["formula"],
                             {"bytes": r["bytes"], "edits": r["edits"],
                              "articles": r["articles"], "quality": quality}) if r else None
        all_comments = []
        for art, ad in a.items():
            for c in ad.get("comments", []):
                all_comments.append(f"[{art}] {c['jury']} ({c['date']}): {c['text']}")
        rows.append((user, r, all_comments, quality, n, score))
    rows.sort(key=lambda x: -(x[5] if (qual and x[5] is not None)
                              else (x[1]["bytes"] if x[1] else -1)))

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=",")
    head = ["#", t["th_u"]]
    if quant:
        head += [t["th_b"]] + hosts + [t["th_e"]]
    if qual:
        head += [t["th_avg"], t["th_score"], t["comments"]]
    w.writerow(head)
    for i, (user, r, all_comments, quality, n, score) in enumerate(rows, 1):
        line = [i, user]
        if quant:
            line.append(r["bytes"] if r else "")
            for h in hosts:
                pp = (r or {}).get("perProject", {}).get(h)
                line.append(pp.get("bytes", t["err"]) if pp else "")
            line.append(f"{r['edits']} / {r['articles']}" if r else "")
        if qual:
            line += [round(quality, 2) if n else "",
                     round(score, 2) if score is not None else "",
                     " | ".join(all_comments)]
        w.writerow(line)
    safe = re.sub(r"[^\w \-]", "", contest["name"], flags=re.U).strip().replace(" ", "_") or "contest"
    ascii_fallback = re.sub(r"[^A-Za-z0-9_\-]", "", safe)
    if not re.search(r"[A-Za-z0-9]", ascii_fallback):
        ascii_fallback = "contest"
    utf8_name = quote(safe + ".csv")
    return Response("\ufeff" + buf.getvalue(),
                    mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"{ascii_fallback}.csv\"; filename*=UTF-8''{utf8_name}"})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
