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
import random
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
CREATE TABLE IF NOT EXISTS article_reviews(
  contest_id TEXT, participant TEXT, article TEXT, jury TEXT,
  PRIMARY KEY(contest_id, participant, article, jury));
CREATE TABLE IF NOT EXISTS article_assignments(
  contest_id TEXT, participant TEXT, article TEXT, jury TEXT,
  PRIMARY KEY(contest_id, participant, article, jury));
"""

def init_db():
    con = sqlite3.connect(DB_PATH)
    had_reviews = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_reviews'").fetchone()
    con.executescript(SCHEMA)
    if not had_reviews:
        # раніше «перевірено» = є оцінка; переносимо це в явні відмітки
        con.execute("INSERT OR IGNORE INTO article_reviews "
                    "SELECT contest_id, participant, article, jury FROM article_scores")
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
FORMULA_VARS = ("bytes", "edits", "articles", "quality", "created", "expanded")

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
   filter_all="Усі", filter_created="Лише нові", filter_expanded="Лише доповнені",
   jury_no_check="Запуск перевірки доступний адміну. Результати вже завантажені.",
   view="Переглянути", open_article="Відкрити статтю", loading="Завантаження…",
   my_pass_title="Мій пароль",
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
   filter_all="All", filter_created="New only", filter_expanded="Expanded only",
   jury_no_check="Running the check is admin-only. Results are already loaded.",
   view="View", open_article="Open article", loading="Loading…",
   my_pass_title="My password",
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
   filter_all="Wszystkie", filter_created="Tylko nowe", filter_expanded="Tylko rozbudowane",
   jury_no_check="Uruchomienie sprawdzenia dostępne tylko dla admina. Wyniki są już załadowane.",
   view="Zobacz", open_article="Otwórz artykuł", loading="Ładowanie…",
   my_pass_title="Moje hasło",
   footer="Wkład ilościowy = suma dodanych bajtów przez API MediaWiki."),
}

# ── Додаткові рядки інтерфейсу (статуси перевірки, розподіл, нові/доповнені) ──
_EXTRA = {
 "uk": dict(
   th_split="Нові / доповн.", th_split_b="Байти: нові / доповнені",
   reviewed="Перевірено", unreviewed="Не перевірено",
   mark_rev="Позначити перевіреною", unmark_rev="Зняти позначку", rev_next="Перевірено → далі",
   prev="Назад", next="Далі",
   tab_preview="Превʼю", tab_article="Стаття", tab_history="Історія",
   open_new="Відкрити в новій вкладці ↗",
   iframe_hint="Якщо вікі не показується у вікні — відкрийте статтю в новій вкладці.",
   filter_todo="Не перевірені", filter_done="Перевірені", filter_mine="Призначені мені",
   mine="мої", assigned_to="Призначено",
   distribute="Розподілити між журі",
   distribute_h="Рівномірно розподіляє статті між членами журі; уже призначені статті зберігаються.",
   per_article="Журі на статтю", reset_dist="Перерозподілити з нуля",
   dist_done="Розподілено статей", load_title="Розподіл і прогрес журі",
   th_jury="Журі", th_assigned="Призначено", th_reviewed="Перевірено",
   th_progress="Перевірено мною", my_assigned="мої призначені",
   no_jury="Немає облікових записів журі.", no_results="Спершу запустіть «Перевірити внесок».",
   formula_h="Змінні: bytes (байти), edits (редагування), articles (сторінки), quality (сер. оцінка журі), created (нові статті), expanded (доповнені статті)"),
 "en": dict(
   th_split="New / expanded", th_split_b="Bytes: new / expanded",
   reviewed="Reviewed", unreviewed="Not reviewed",
   mark_rev="Mark as reviewed", unmark_rev="Remove mark", rev_next="Reviewed → next",
   prev="Back", next="Next",
   tab_preview="Preview", tab_article="Article", tab_history="History",
   open_new="Open in a new tab ↗",
   iframe_hint="If the wiki does not load in the frame, open the article in a new tab.",
   filter_todo="Not reviewed", filter_done="Reviewed", filter_mine="Assigned to me",
   mine="mine", assigned_to="Assigned to",
   distribute="Distribute among jury",
   distribute_h="Distributes articles evenly among jury members; existing assignments are kept.",
   per_article="Jurors per article", reset_dist="Redistribute from scratch",
   dist_done="Articles distributed", load_title="Jury distribution and progress",
   th_jury="Jury", th_assigned="Assigned", th_reviewed="Reviewed",
   th_progress="Reviewed by me", my_assigned="my assigned",
   no_jury="There are no jury accounts.", no_results="Run “Check contributions” first.",
   formula_h="Variables: bytes, edits, articles, quality (avg jury score), created (new articles), expanded (expanded articles)"),
 "pl": dict(
   th_split="Nowe / rozbud.", th_split_b="Bajty: nowe / rozbudowane",
   reviewed="Sprawdzono", unreviewed="Niesprawdzone",
   mark_rev="Oznacz jako sprawdzone", unmark_rev="Usuń oznaczenie", rev_next="Sprawdzone → dalej",
   prev="Wstecz", next="Dalej",
   tab_preview="Podgląd", tab_article="Artykuł", tab_history="Historia",
   open_new="Otwórz w nowej karcie ↗",
   iframe_hint="Jeśli wiki nie wczytuje się w oknie, otwórz artykuł w nowej karcie.",
   filter_todo="Niesprawdzone", filter_done="Sprawdzone", filter_mine="Przydzielone mnie",
   mine="moje", assigned_to="Przydzielono",
   distribute="Rozdziel między jury",
   distribute_h="Równomiernie rozdziela artykuły między członków jury; istniejące przydziały są zachowane.",
   per_article="Jurorów na artykuł", reset_dist="Rozdziel od nowa",
   dist_done="Rozdzielono artykułów", load_title="Podział i postęp jury",
   th_jury="Jury", th_assigned="Przydzielono", th_reviewed="Sprawdzono",
   th_progress="Sprawdzone przeze mnie", my_assigned="moje przydzielone",
   no_jury="Brak kont jury.", no_results="Najpierw uruchom „Sprawdź wkład”.",
   formula_h="Zmienne: bytes (bajty), edits (edycje), articles (strony), quality (śr. ocena jury), created (nowe artykuły), expanded (rozbudowane artykuły)"),
}
for _l, _d in _EXTRA.items():
    T[_l].update(_d)
T["uk"].update(legacy_title="Перевірено до оновлення",
               legacy_h="Внесок оцінено в попередній версії: оцінка враховується в якості, статті не розподіляються.")
T["en"].update(legacy_title="Reviewed before the update",
               legacy_h="This contribution was assessed in the previous version: its score counts toward quality, and its articles are not distributed.")
T["pl"].update(legacy_title="Sprawdzono przed aktualizacją",
               legacy_h="Ten wkład oceniono w poprzedniej wersji: ocena liczy się do jakości, a artykuły nie są rozdzielane.")

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
.st-ok{background:#e6f4ea;color:#1d7a4f}.st-todo{background:#fdf0dc;color:#9a5b00}
.md-back{position:fixed;inset:0;background:rgba(16,24,32,.6);z-index:1000;display:flex;align-items:center;justify-content:center;padding:12px}
.md-box{background:#fff;border-radius:14px;width:100%;max-width:1360px;height:94vh;display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,1fr);overflow:hidden}
.md-left{display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
.md-right{padding:16px 18px;overflow:auto}
.md-tabs{display:flex;gap:6px;padding:10px 12px;border-bottom:1px solid var(--line);align-items:center;flex-wrap:wrap}
.md-tabs button{background:#fff;border:1px solid var(--line);color:var(--ink2);padding:5px 12px}
.md-tabs button.on{background:var(--grad-dark);color:#fff;font-weight:600}
.md-pane{flex:1;overflow:auto;padding:16px 18px}
.md-frame{flex:1;width:100%;border:0;min-height:0}
.md-top{display:flex;gap:8px;align-items:center;margin-bottom:10px}
@media(max-width:820px){.md-box{grid-template-columns:1fr;overflow:auto;height:96vh}.md-left{height:62vh}}
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
function sleep(ms){return new Promise(res=>setTimeout(res,ms))}
async function mw(api,params,tries){
 tries=tries||0;
 const p=new URLSearchParams({format:'json',origin:'*',...params});
 const r=await fetch(api+'?'+p);
 if(r.status===429||r.status===503){
  if(tries>=6)throw new Error('HTTP '+r.status+' (ліміт запитів вікі, спроби вичерпано)');
  const ra=parseFloat(r.headers.get('Retry-After'))||0;
  await sleep(Math.max(ra*1000,800*Math.pow(2,tries)));
  return mw(api,params,tries+1);
 }
 if(!r.ok)throw new Error('HTTP '+r.status);
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
   rvprop:'user|timestamp|size|flags|ids',rvlimit:'500',rvdir:'older'};
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
  let failed=0;
  for(let i=0;i<pages.length;i++){
   pbar(bar,i,pages.length,pages[i]);
   try{
    const params={action:'query',prop:'revisions',titles:pages[i],rvprop:'user|timestamp',rvlimit:'500'};
    if(sISO)params.rvend=sISO;if(eISO)params.rvstart=eISO;
    const d=await mw(tp.api,params);
    const page=Object.values(d.query?.pages||{})[0];
    (page?.revisions||[]).forEach(r=>{if(r.user&&!/bot$|бот$/i.test(r.user.trim())&&!isAnon(r.user.trim()))users.add(r.user)});
   }catch(e){failed++}
   if(i%5===4)await sleep(150);
  }
  pbar(bar,pages.length,pages.length,'');
  const ta=document.getElementById('parts');
  const existing=ta.value.split('\\n').map(s=>s.trim()).filter(Boolean);
  ta.value=[...new Set([...existing,...users])].sort((a,b)=>a.localeCompare(b)).join('\\n');
  msg.textContent='✓ '+pages.length+' / '+users.size+(failed?' ('+failed+' сторінок пропущено через помилку)':'');
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
      if('new' in r||r.parentid===0)pp._detail[tplPages[i]].created=true;
     });
    }catch(e){/* сторінку могли видалити/перейменувати — пропускаємо */}
    if(i%5===4)await sleep(150);
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
    if(i%5===4)await sleep(150);
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
let ART_FILTER='all'; // 'all' | 'created' | 'expanded'
let REV_FILTER='all'; // 'all' | 'todo' | 'done' | 'mine'
let CUR_LIST=[];      // поточний відфільтрований список статей відкритого учасника
let SCROLL_RESET=false;
let MTAB='article';   // 'preview' | 'article' | 'history'
const JURY_LIST={{juryjson|safe}};
const LEGACY={{ljson|safe}}; // оцінки/коментарі з попередньої версії (рівень учасника)
function legacyDone(user){const l=LEGACY[user];return !!(l&&(Object.keys(l.scores||{}).length||(l.comments||[]).length))}
function legacyVals(user){const l=LEGACY[user];return l?Object.values(l.scores||{}).filter(v=>v!=null):[]}
function setArtFilter(f){ART_FILTER=f;SCROLL_RESET=true;render()}
function setRevFilter(f){REV_FILTER=f;SCROLL_RESET=true;render()}
function articleLink(origin,title){return origin+'/wiki/'+encodeURIComponent(title.replace(/ /g,'_'))}
function slot(user,title){const a=ASSESS[user]&&ASSESS[user][title];return a||{scores:{},comments:[],reviewed:[],assigned:[]}}
function isReviewed(user,title){return legacyDone(user)||(slot(user,title).reviewed||[]).indexOf(JURY_LOGIN)>=0}
function isMine(user,title){return (slot(user,title).assigned||[]).indexOf(JURY_LOGIN)>=0}
function juryName(login){const j=JURY_LIST.find(x=>x.login===login);return j?j.name:login}
async function reloadState(){const r=await fetch('/api/state/'+CID);ASSESS=(await r.json()).assess}
async function setReviewed(user,title,val){
 await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({contest:CID,participant:user,article:title,reviewed:!!val})});
 await reloadState();render();
}
async function runDistribute(){
 const reset=document.getElementById('distReset').checked;
 const per=Number(document.getElementById('distPer').value)||1;
 const msg=document.getElementById('distmsg');
 msg.textContent=T.loading;
 try{
  const r=await fetch('/api/distribute/'+CID,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({per_article:per,reset:reset})});
  const d=await r.json();
  if(!r.ok||d.error){msg.textContent='✗ '+(T[d.error]||d.error||r.status);return}
  await reloadState();render();
  msg.textContent='✓ '+T.dist_done+': '+d.items;
 }catch(e){msg.textContent='✗ '+e.message}
}
function renderLoad(){
 const box=document.getElementById('loadbox');if(!box)return;
 box.style.display=QUAL?'block':'none';if(!QUAL)return;
 const st={};
 JURY_LIST.forEach(j=>{if(j.role==='jury')st[j.login]={a:0,r:0}});
 Object.values(ASSESS).forEach(pa=>Object.values(pa).forEach(a=>{
  (a.assigned||[]).forEach(l=>{if(!st[l])st[l]={a:0,r:0};st[l].a++;if((a.reviewed||[]).indexOf(l)>=0)st[l].r++})}));
 const logins=Object.keys(st);
 let h;
 if(!logins.length)h='<div class="hint">'+T.no_jury+'</div>';
 else{
  h='<table><thead><tr><th>'+T.th_jury+'</th><th>'+T.th_assigned+'</th><th>'+T.th_reviewed+'</th><th style="width:35%"></th></tr></thead><tbody>';
  logins.forEach(l=>{const s=st[l];const pct=s.a?Math.round(s.r/s.a*100):0;
   h+='<tr><td><b>'+esc(juryName(l))+'</b></td><td class="mono">'+s.a+'</td><td class="mono">'+s.r+' ('+pct+'%)</td><td><div class="pbar"><i style="width:'+pct+'%"></i></div></td></tr>'});
  h+='</tbody></table>';
 }
 document.getElementById('loadtbl').innerHTML=h;
}

// ── Модальне вікно: стаття (iframe) / превʼю / історія зліва, оцінка та коментарі справа ──
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
function openIdx(i){const it=CUR_LIST[i];if(it)openItem(it)}
function modalIdx(){
 if(!MODAL)return -1;
 return CUR_LIST.findIndex(x=>x.user===MODAL.user&&x.title===MODAL.title&&x.host===MODAL.host);
}
function stepModal(d){const i=modalIdx();const n=i>=0?CUR_LIST[i+d]:null;if(n)openItem(n)}
async function reviewAndNext(){
 if(!MODAL)return;
 const i=modalIdx();const nxt=i>=0?CUR_LIST[i+1]:null;const m=MODAL;
 if(!isReviewed(m.user,m.title))await setReviewed(m.user,m.title,true);
 if(nxt)openItem(nxt);else closeModal();
}
function toggleReviewed(){if(MODAL)setReviewed(MODAL.user,MODAL.title,!isReviewed(MODAL.user,MODAL.title))}
function saveScoreM(v){if(MODAL)saveScore(MODAL.user,MODAL.title,v)}
async function saveCommentM(){
 const inp=document.querySelector('.md-cm');if(!inp||!MODAL)return;
 const text=inp.value.trim();if(!text)return;
 inp.value='';
 await fetch('/api/comment',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({contest:CID,participant:MODAL.user,article:MODAL.title,text})});
 await reloadState();render();
}
function setTab(k){MTAB=k;renderModal()}
async function openItem(it){
 MODAL={user:it.user,title:it.title,origin:it.origin,host:it.host,api:it.api,label:it.label,bytes:it.bytes,created:it.created};
 renderModal();
 const key=it.host+'::'+it.title;
 const cached=EXTRACT_CACHE[key];
 if(!cached||cached.extract==null){
  try{
   const d=await mw(it.api,{action:'query',prop:'extracts|pageimages',exintro:'1',explaintext:'1',
    exchars:'700',piprop:'thumbnail',pithumbsize:'500',redirects:'1',titles:it.title});
   const page=Object.values(d.query?.pages||{})[0];
   EXTRACT_CACHE[key]={extract:page?.extract||'',thumb:page?.thumbnail?.source||(cached&&cached.thumb)||null};
  }catch(e){EXTRACT_CACHE[key]={extract:'',thumb:(cached&&cached.thumb)||null,err:String(e.message||e)}}
  if(MODAL&&MODAL.title===it.title&&MODAL.host===it.host)renderModal();
 }
}
function closeModal(){MODAL=null;renderModal()}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&MODAL)closeModal()});
function leftHtml(ex){
 const m=MODAL;
 let h='<div class="md-tabs">';
 [['article',T.tab_article],['history',T.tab_history],['preview',T.tab_preview]].forEach(x=>{
  h+='<button data-tab="'+x[0]+'" class="'+(MTAB===x[0]?'on':'')+'">'+esc(x[1])+'</button>'});
 h+='<a class="hint" style="margin-left:auto" target="_blank" rel="noopener" title="'+esc(T.iframe_hint)+'" href="'+esc(articleLink(m.origin,m.title))+'">'+T.open_new+'</a></div>';
 if(MTAB==='preview'){
  h+='<div class="md-pane">';
  if(!ex||ex.extract==null)h+='<div class="hint">'+T.loading+'</div>';
  else if(ex.err)h+='<div class="hint">'+esc(ex.err)+'</div>';
  else{
   if(ex.thumb)h+='<img src="'+esc(ex.thumb)+'" alt="" style="max-width:100%;border-radius:8px;margin-bottom:10px;display:block">';
   h+='<h3 style="margin:0 0 8px">'+esc(m.title)+'</h3>';
   h+='<div style="font-size:14px;line-height:1.55;white-space:pre-wrap">'+esc(ex.extract||'')+'</div>';
  }
  h+='</div>';
 }else{
  const enc=encodeURIComponent(m.title.replace(/ /g,'_'));
  const src=MTAB==='history'?m.origin+'/w/index.php?title='+enc+'&action=history':articleLink(m.origin,m.title)+'?useskin=minerva';
  h+='<iframe class="md-frame" src="'+esc(src)+'" sandbox="allow-same-origin allow-scripts allow-popups allow-popups-to-escape-sandbox"></iframe>';
 }
 return h;
}
function rightHtml(){
 const m=MODAL;
 const ad=slot(m.user,m.title);
 const vals=Object.values(ad.scores||{}).filter(v=>v!=null);
 const avg=vals.length?(vals.reduce((x,y)=>x+y,0)/vals.length):null;
 const my=(ad.scores||{})[JURY_LOGIN];
 const rev=isReviewed(m.user,m.title);
 const idx=modalIdx();
 const cid=idFor(m.user+'||'+m.title);
 let h='<div class="md-top">';
 h+='<button class="btn-s" onclick="stepModal(-1)"'+(idx>0?'':' disabled')+'>‹ '+T.prev+'</button>';
 h+='<button class="btn-s" onclick="stepModal(1)"'+(idx>=0&&idx<CUR_LIST.length-1?'':' disabled')+'>'+T.next+' ›</button>';
 h+='<span class="hint mono">'+(idx>=0?(idx+1)+' / '+CUR_LIST.length:'')+'</span>';
 h+='<button class="btn-s" style="margin-left:auto;padding:4px 10px" onclick="closeModal()">✕</button></div>';
 h+='<h3 style="margin:4px 0 6px">'+esc(m.title)+'</h3>';
 h+='<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
 h+='<span class="chip" style="background:'+(m.created?'#e6f4ea':'var(--acc-soft)')+';color:'+(m.created?'#1d7a4f':'var(--acc)')+'">'+(m.created?T.created:T.expanded)+'</span>';
 h+='<span class="chip '+(rev?'st-ok':'st-todo')+'">'+(rev?'✓ '+T.reviewed:'● '+T.unreviewed)+'</span>';
 h+='<span class="hint mono">'+esc(m.label)+' · +'+fmtN(m.bytes)+(LANG==='uk'?' Б':' B')+'</span></div>';
 h+='<div class="hint">'+esc(m.user)+'</div>';
 if((ad.assigned||[]).length)h+='<div class="hint" style="margin-top:4px">'+T.assigned_to+': '+ad.assigned.map(l=>esc(juryName(l))).join(', ')+'</div>';
 h+='<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px">';
 h+='<label class="hint" style="font-size:12px">'+T.th_my+':</label>';
 h+='<input type="number" step="0.1" style="width:70px" value="'+(my??'')+'" onchange="saveScoreM(this.value)">';
 h+='<span class="hint mono">'+T.th_avg+': '+(avg!=null?avg.toFixed(2):'—')+(vals.length?' ('+vals.length+' '+T.scores_n+')':'')+'</span></div>';
 h+='<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">';
 h+='<button class="btn-p" onclick="reviewAndNext()">'+(rev?T.next+' ›':T.rev_next)+'</button>';
 h+=(legacyDone(m.user)?'':'<button class="btn-s" onclick="toggleReviewed()">'+(rev?T.unmark_rev:T.mark_rev)+'</button>')+'</div>';
 h+='<div style="margin-top:14px">';
 (ad.comments||[]).forEach(c=>{h+='<div style="padding:5px 0;border-bottom:1px dashed var(--line)"><b>'+esc(c.jury)+'</b> <span class="hint mono">'+esc(c.date)+'</span><div>'+esc(c.text)+'</div></div>'});
 if(!(ad.comments||[]).length)h+='<div class="hint" style="font-size:12px">'+T.no_comments+'</div>';
 h+='<div style="display:flex;gap:8px;margin-top:8px"><input class="md-cm" id="cm_'+cid+'" placeholder="'+esc(T.comment_ph)+'" onkeydown="if(event.keyCode===13)saveCommentM()"><button class="btn-p" onclick="saveCommentM()">'+T.save+'</button></div>';
 h+='</div>';
 return h;
}
function renderModal(){
 let el=document.getElementById('artModal');
 if(!MODAL){if(el)el.remove();return}
 if(!el){
  el=document.createElement('div');el.id='artModal';
  el.innerHTML='<div class="md-back" onclick="if(event.target===this)closeModal()"><div class="md-box"><div class="md-left" id="mdLeft"></div><div class="md-right" id="mdRight"></div></div></div>';
  document.body.appendChild(el);
 }
 const key=MODAL.host+'::'+MODAL.title;
 const ex=EXTRACT_CACHE[key];
 // ліва частина перебудовується лише при зміні статті/вкладки — щоб iframe не перезавантажувався
 const left=document.getElementById('mdLeft');
 const lk=key+'|'+MTAB+'|'+(MTAB==='preview'?(ex&&ex.extract!=null?'1':'0'):'');
 if(left.dataset.k!==lk){
  left.dataset.k=lk;left.innerHTML=leftHtml(ex);
  left.querySelectorAll('[data-tab]').forEach(b=>{b.onclick=()=>setTab(b.dataset.tab)});
 }
 const rt=document.getElementById('mdRight');
 const old=rt.querySelector('.md-cm');
 const draft=old?old.value:'';const hadFocus=!!old&&document.activeElement===old;
 const same=rt.dataset.k===key;
 rt.innerHTML=rightHtml();rt.dataset.k=key;
 const nw=rt.querySelector('.md-cm');
 if(nw&&same){nw.value=draft;if(hadFocus)nw.focus()}
}
function articleQuality(pa,user){
 if(!pa)return{quality:0,n:0};
 const avgs=[];let n=0;
 Object.values(pa).forEach(a=>{
  const vals=Object.values(a.scores||{}).filter(v=>v!=null);
  if(vals.length){avgs.push(vals.reduce((x,y)=>x+y,0)/vals.length);n+=vals.length}
 });
 if(!avgs.length&&user){const lv=legacyVals(user);if(lv.length)return{quality:lv.reduce((x,y)=>x+y,0)/lv.length,n:lv.length}}
 return{quality:avgs.length?avgs.reduce((x,y)=>x+y,0)/avgs.length:0,n};
}
function evalFormula(f,v){
 const cleaned=f.replace(/\\b(bytes|edits|articles|quality|created|expanded)\\b/g,'1');
 if(!/^[\\d\\s+\\-*/().,]+$/.test(cleaned))return null;
 try{const fn=new Function('bytes','edits','articles','quality','created','expanded','return ('+f+')');
  const r=fn(v.bytes,v.edits,v.articles,v.quality,v.created||0,v.expanded||0);
  return typeof r==='number'&&!isNaN(r)?r:null}catch(e){return null}
}
// розбивка внеску на нові та доповнені статті (з уже збережених списків)
function splitStats(r){
 const s={created:0,expanded:0,bc:0,be:0};
 if(!r)return s;
 Object.values(r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{
  if(it.created){s.created++;s.bc+=it.bytes}else{s.expanded++;s.be+=it.bytes}}));
 return s;
}
function partTitles(r){
 const s=new Set();
 if(r)Object.values(r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>s.add(it.title)));
 return[...s];
}
function fmtN(n){return n==null?'—':n.toLocaleString(LANG==='uk'?'uk-UA':'en-GB')}
function idFor(u){return btoa(unescape(encodeURIComponent(u))).replace(/[^a-z0-9]/gi,'')}
function contribLink(origin,user){return origin+'/wiki/Special:Contributions/'+encodeURIComponent(user)}

let ROWS_CUR=[];
function toggleArt(i){const u=ROWS_CUR[i].user;ARTOPEN=ARTOPEN===u?null:u;SCROLL_RESET=true;render()}
function render(){
 const el=document.getElementById('tbl');if(!CONTEST){el.innerHTML='';return}
 const oldCar=el.querySelector('.carousel');
 const keepScroll=(oldCar&&!SCROLL_RESET)?oldCar.scrollLeft:0;SCROLL_RESET=false;
 CUR_LIST=[];
 const rows=CONTEST.participants.map(user=>{
  const r=RESULTS?RESULTS[user]:null;const pa=ASSESS[user]||{};
  const{quality,n}=articleQuality(pa,user);
  const sp=splitStats(r);
  const score=r?evalFormula(CONTEST.formula,{bytes:r.bytes,edits:r.edits,articles:r.articles,quality,created:sp.created,expanded:sp.expanded}):null;
  const titles=partTitles(r);
  const done=titles.filter(t=>isReviewed(user,t)).length;
  const mineT=titles.filter(t=>isMine(user,t));
  const mineDone=mineT.filter(t=>isReviewed(user,t)).length;
  return{user,r,pa,quality,n,score,sp,total:titles.length,done,mine:mineT.length,mineDone};
 }).sort((x,y)=>QUAL?((y.score??-1e18)-(x.score??-1e18)):((y.r?y.r.bytes:-1)-(x.r?x.r.bytes:-1)));
 ROWS_CUR=rows;
 const maxB=Math.max(1,...rows.map(r=>r.r?r.r.bytes:0));
 let h='<table><thead><tr><th>#</th><th>'+T.th_u+'</th>';
 if(QUANT){h+='<th>'+T.th_b+'</th>';projects.forEach(p=>h+='<th>'+esc(p.label)+'</th>');h+='<th>'+T.th_e+'</th><th>'+T.th_split+'</th>'}
 if(QUAL)h+='<th>'+T.th_avg+'</th><th>'+T.th_score+'</th><th>'+T.th_progress+'</th>';
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
   h+='<td class="mono" style="font-size:12px">'+(row.r?'<span style="color:#1d7a4f">'+fmtN(row.sp.created)+'</span> / '+fmtN(row.sp.expanded)
    +'<div class="hint">+'+fmtN(row.sp.bc)+' / +'+fmtN(row.sp.be)+'</div>':'—')+'</td>';
  }
  if(QUAL){
   h+='<td class="mono">'+(row.n?row.quality.toFixed(2):'—')+'<div class="hint">'+row.n+' '+T.scores_n+'</div></td>';
   h+='<td><span class="mono" style="font-weight:700;color:var(--gold);font-size:15px">'+(row.score!=null?row.score.toFixed(2):'—')+'</span></td>';
   h+='<td class="mono" style="font-size:12px">'+(row.total?row.done+' / '+row.total:'—')
    +(row.mine?'<div class="hint">'+T.my_assigned+': '+row.mineDone+' / '+row.mine+'</div>':'')+'</td>';
  }
  h+='<td>'+(QUAL?'<button class="btn-s" style="padding:4px 10px;font-size:12px" onclick="toggleArt('+i+')">'+T.articles_btn+(row.r?' ('+row.r.articles+')':'')+'</button>':'')+'</td></tr>';
  if(QUAL&&ARTOPEN===row.user){
   h+='<tr><td colspan="99" style="background:#fafbfc"><div style="max-width:1000px">';
   if(legacyDone(row.user)){
    const lg=LEGACY[row.user],lv=legacyVals(row.user);
    h+='<div style="margin-bottom:10px;padding:10px 12px;border-radius:10px;background:#e6f4ea"><b>✓ '+T.legacy_title+'</b>'
     +(lv.length?' · '+T.th_avg+': '+(lv.reduce((x,y)=>x+y,0)/lv.length).toFixed(2)+' ('+lv.length+' '+T.scores_n+')':'');
    (lg.comments||[]).forEach(c=>{h+='<div style="padding:3px 0"><b>'+esc(c.jury)+'</b> <span class="hint mono">'+esc(c.date)+'</span> '+esc(c.text)+'</div>'});
    h+='<div class="hint" style="margin-top:4px">'+T.legacy_h+'</div></div>';
   }
   const items=[];
   projects.forEach(p=>{const pp=row.r?row.r.perProject[p.host]:null;
    (pp&&pp.list?pp.list:[]).forEach(it=>items.push({...it,origin:p.origin,label:p.label,host:p.host,api:p.api}))});
   // спершу неперевірені мною, серед них — призначені мені, далі за приростом байтів
   items.sort((a,b2)=>{
    const ra=isReviewed(row.user,a.title)?1:0,rb=isReviewed(row.user,b2.title)?1:0;
    if(ra!==rb)return ra-rb;
    const ma=isMine(row.user,a.title)?1:0,mb=isMine(row.user,b2.title)?1:0;
    if(ma!==mb)return mb-ma;
    return b2.bytes-a.bytes;
   });
   if(!items.length)h+='<div class="hint">'+T.no_articles+'</div>';
   else{
    const byHost={};
    items.forEach(it=>{(byHost[it.host]=byHost[it.host]||{api:it.api,t:[]}).t.push(it.title)});
    Object.keys(byHost).forEach(hh=>prefetchThumbs(byHost[hh].api,hh,byHost[hh].t));
    const nC=items.filter(it=>it.created).length,nE=items.length-nC;
    const nDone=items.filter(it=>isReviewed(row.user,it.title)).length;
    const nMine=items.filter(it=>isMine(row.user,it.title)).length;
    const btn=(cls,f,cur,lbl)=>'<button class="btn-s '+cls+'" data-f="'+f+'" style="padding:3px 10px;font-size:12px'+(cur===f?';background:var(--grad-dark);color:#fff':'')+'">'+esc(lbl)+'</button>';
    h+='<div style="display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap">';
    h+=btn('art-filt-btn','all',ART_FILTER,T.filter_all+' ('+items.length+')');
    h+=btn('art-filt-btn','created',ART_FILTER,T.filter_created+' ('+nC+')');
    h+=btn('art-filt-btn','expanded',ART_FILTER,T.filter_expanded+' ('+nE+')');
    h+='</div><div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">';
    h+=btn('rev-filt-btn','all',REV_FILTER,T.filter_all);
    h+=btn('rev-filt-btn','todo',REV_FILTER,T.filter_todo+' ('+(items.length-nDone)+')');
    h+=btn('rev-filt-btn','done',REV_FILTER,T.filter_done+' ('+nDone+')');
    h+=btn('rev-filt-btn','mine',REV_FILTER,T.filter_mine+' ('+nMine+')');
    h+='</div>';
    const filtered=items.filter(it=>{
     if(ART_FILTER==='created'&&!it.created)return false;
     if(ART_FILTER==='expanded'&&it.created)return false;
     const rv=isReviewed(row.user,it.title);
     if(REV_FILTER==='todo'&&rv)return false;
     if(REV_FILTER==='done'&&!rv)return false;
     if(REV_FILTER==='mine'&&!isMine(row.user,it.title))return false;
     return true;
    });
    CUR_LIST=filtered.map(it=>({...it,user:row.user}));
    if(!filtered.length)h+='<div class="hint">'+T.no_articles+'</div>';
    else{
     h+='<div class="carousel-wrap">';
     h+='<button class="car-nav prev" onclick="this.nextElementSibling.scrollBy({left:-320,behavior:\\'smooth\\'})">‹</button>';
     h+='<div class="carousel">';
     filtered.forEach((it,k)=>{
      const ad=slot(row.user,it.title);
      const vals=Object.values(ad.scores||{}).filter(v=>v!=null);
      const avg=vals.length?(vals.reduce((x,y)=>x+y,0)/vals.length):null;
      const nCm=(ad.comments||[]).length;
      const rv=isReviewed(row.user,it.title),mine=isMine(row.user,it.title);
      const ex=EXTRACT_CACHE[it.host+'::'+it.title];
      const thumbHtml=ex&&ex.thumb?'<img class="thumb" src="'+esc(ex.thumb)+'" alt="">':'<div class="thumb-ph">'+(ex?'—':'…')+'</div>';
      h+='<div class="art-card" style="'+(rv?'opacity:.72':'')+'" onclick="openIdx('+k+')">';
      h+=thumbHtml;
      h+='<div class="body"><div class="art-title">'+esc(it.title)+'</div>';
      h+='<div class="hint" style="font-size:10px;margin-top:4px">'+esc(it.label)+' · +'+fmtN(it.bytes)+(LANG==='uk'?' Б':' B')+'</div>';
      h+='<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:5px">';
      h+='<span class="chip" style="font-size:10px;background:'+(it.created?'#e6f4ea':'var(--acc-soft)')+';color:'+(it.created?'#1d7a4f':'var(--acc)')+'">'+(it.created?T.created:T.expanded)+'</span>';
      h+='<span class="chip '+(rv?'st-ok':'st-todo')+'" style="font-size:10px">'+(rv?'✓ '+T.reviewed:'● '+T.unreviewed)+'</span>';
      if(mine)h+='<span class="chip tag-a" style="font-size:10px">'+T.mine+'</span>';
      if(avg!=null)h+='<span class="chip" style="font-size:10px;background:#fbf3e2;color:#b07a1e">★ '+avg.toFixed(1)+'</span>';
      if(nCm)h+='<span class="hint" style="font-size:10px">💬'+nCm+'</span>';
      h+='</div></div></div>';
     });
     h+='</div>';
     h+='<button class="car-nav next" onclick="this.previousElementSibling.scrollBy({left:320,behavior:\\'smooth\\'})">›</button>';
     h+='</div>';
    } // end if filtered.length
   } // end else items.length
   h+='</div></td></tr>';
  }
 });
 h+='</tbody></table>';
 el.innerHTML=h;
 const nc=el.querySelector('.carousel');if(nc&&keepScroll)nc.scrollLeft=keepScroll;
 el.querySelectorAll('.art-filt-btn').forEach(b=>{b.onclick=function(){setArtFilter(this.dataset.f)}});
 el.querySelectorAll('.rev-filt-btn').forEach(b=>{b.onclick=function(){setRevFilter(this.dataset.f)}});
 document.getElementById('csvlink').href='/export/'+CID+'.csv?quant='+(QUANT?1:0)+'&qual='+(QUAL?1:0);
 renderLoad();
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
   {% if me.role=='admin' %}
   <button id="runbtn" class="btn-p" onclick="runCheck()">{{t.check}}</button>
   {% else %}
   <span class="hint" style="align-self:center;font-size:11px;max-width:200px;text-align:right">{{t.jury_no_check}}</span>
   {% endif %}
  </div>
  <div id="runbar" style="margin-top:6px"></div>
 </div>
</div>
<div id="loadbox" class="card" style="display:none">
 <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
  <div><div class="lbl" style="margin:0">{{t.load_title}}</div><div class="hint">{{t.distribute_h}}</div></div>
  {% if me.role=='admin' %}
  <div style="margin-left:auto;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
   <div><label class="lbl" style="margin:0 0 2px">{{t.per_article}}</label><input id="distPer" type="number" min="1" value="1" style="width:70px"></div>
   <label class="hint" style="display:flex;align-items:center;gap:6px;margin-bottom:8px"><input type="checkbox" id="distReset" style="width:auto"> {{t.reset_dist}}</label>
   <button class="btn-p" onclick="runDistribute()">{{t.distribute}}</button>
  </div>
  {% endif %}
 </div>
 <div id="distmsg" class="hint mono" style="margin-top:6px"></div>
 <div id="loadtbl" style="margin-top:8px"></div>
</div>
<div class="card" style="padding:0;overflow-x:auto"><div id="tbl"></div></div>
<div class="hint">{{t.f_of}} <span class="chip">{{contest.formula}}</span></div>
{% endif %}
{% if me.role=='jury' %}
<div class="card" style="max-width:400px;margin-top:14px">
 <h3 style="margin-top:0">{{t.my_pass_title}} — {{me.name}}</h3>
 <form method="post" action="{{url_for('change_password')}}">
  <label class="lbl">{{t.new_pass}}</label><input name="p1" type="password" required minlength="4">
  <label class="lbl">{{t.again}}</label><input name="p2" type="password" required minlength="4">
  <button class="btn-p" style="margin-top:12px">{{t.save}}</button>
 </form>
</div>
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
    """Оцінки, коментарі, статуси перевірки та призначення по кожній статті.
    Структура: {учасник: {стаття: {"scores":{журі:бал}, "comments":[...],
                                   "reviewed":[журі], "assigned":[журі]}}}"""
    assess = {}

    def slot(p, a):
        return assess.setdefault(p, {}).setdefault(
            a, {"scores": {}, "comments": [], "reviewed": [], "assigned": []})

    for r in db().execute("SELECT * FROM article_scores WHERE contest_id=?", (cid,)):
        slot(r["participant"], r["article"])["scores"][r["jury"]] = r["value"]
    for r in db().execute(
            "SELECT * FROM article_comments WHERE contest_id=? ORDER BY id", (cid,)):
        slot(r["participant"], r["article"])["comments"].append(
            {"jury": r["jury_name"], "text": r["text"], "date": r["date"]})
    for r in db().execute("SELECT * FROM article_reviews WHERE contest_id=?", (cid,)):
        slot(r["participant"], r["article"])["reviewed"].append(r["jury"])
    for r in db().execute(
            "SELECT * FROM article_assignments WHERE contest_id=? ORDER BY jury", (cid,)):
        slot(r["participant"], r["article"])["assigned"].append(r["jury"])
    return assess

def split_stats(r):
    """Нові та доповнені статті учасника: кількість і додані байти."""
    s = {"created": 0, "expanded": 0, "bc": 0, "be": 0}
    for pp in ((r or {}).get("perProject") or {}).values():
        for it in (pp.get("list") or []):
            if it.get("created"):
                s["created"] += 1
                s["bc"] += it.get("bytes", 0)
            else:
                s["expanded"] += 1
                s["be"] += it.get("bytes", 0)
    return s

def get_legacy(cid):
    """Оцінки та коментарі, виставлені в попередній версії (на рівні учасника, а не статті).
    Структура: {учасник: {"scores": {журі: бал}, "comments": [...]}}"""
    out = {}

    def slot(p):
        return out.setdefault(p, {"scores": {}, "comments": []})

    for r in db().execute("SELECT participant, jury, value FROM scores WHERE contest_id=?", (cid,)):
        if r["value"] is not None:
            slot(r["participant"])["scores"][r["jury"]] = r["value"]
    for r in db().execute("SELECT participant, jury_name, text, date FROM comments "
                          "WHERE contest_id=? ORDER BY id", (cid,)):
        slot(r["participant"])["comments"].append(
            {"jury": r["jury_name"], "text": r["text"], "date": r["date"]})
    return out

def article_quality(participant_assess, legacy=None):
    """Якість учасника = середнє арифметичне середніх оцінок по кожній статті
    (спершу усереднюємо оцінки журі в межах статті, потім — по статтях).
    Якщо оцінок по статтях немає, але є оцінки з попередньої версії — беремо їх середнє."""
    article_avgs = []
    n = 0
    for a in (participant_assess or {}).values():
        vals = [v for v in (a.get("scores") or {}).values() if v is not None]
        if vals:
            article_avgs.append(sum(vals) / len(vals))
            n += len(vals)
    if article_avgs:
        return sum(article_avgs) / len(article_avgs), n
    old = [v for v in ((legacy or {}).get("scores") or {}).values() if v is not None]
    if old:
        return sum(old) / len(old), len(old)
    return 0.0, 0

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
        ajson=json.dumps(assess, ensure_ascii=False),
        juryjson=json.dumps([{"login": u.login, "name": u.name, "role": u.role} for u in users],
                            ensure_ascii=False).replace("</", "<\\/"),
        ljson=json.dumps(get_legacy(contest.id) if contest else {},
                         ensure_ascii=False).replace("</", "<\\/"))

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
    for tbl in ("contests", "results", "scores", "comments", "article_scores",
                "article_comments", "article_reviews", "article_assignments"):
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
    me = session["user"]["login"]
    if d.get("value") is None:
        db().execute("DELETE FROM article_scores WHERE contest_id=? AND participant=? AND article=? AND jury=?",
                     (d["contest"], d["participant"], article, me))
    else:
        db().execute("""INSERT INTO article_scores VALUES(?,?,?,?,?)
            ON CONFLICT(contest_id,participant,article,jury) DO UPDATE SET value=excluded.value""",
            (d["contest"], d["participant"], article, me, float(d["value"])))
        # виставлена оцінка автоматично означає, що статтю перевірено
        db().execute("INSERT OR IGNORE INTO article_reviews VALUES(?,?,?,?)",
                     (d["contest"], d["participant"], article, me))
    db().commit()
    return jsonify(ok=True)

@app.route("/api/review", methods=["POST"])
@login_required
def api_review():
    d = request.get_json()
    key = (d["contest"], d["participant"], d.get("article", ""), session["user"]["login"])
    if d.get("reviewed"):
        db().execute("INSERT OR IGNORE INTO article_reviews VALUES(?,?,?,?)", key)
    else:
        db().execute("DELETE FROM article_reviews WHERE contest_id=? AND participant=? "
                     "AND article=? AND jury=?", key)
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

@app.route("/api/distribute/<cid>", methods=["POST"])
@login_required
def api_distribute(cid):
    """Рівномірно розподіляє статті конкурсу між журі (лише адмін).
    Вже наявні призначення зберігаються; нові статті йдуть журі з найменшим навантаженням."""
    if session["user"]["role"] != "admin":
        return jsonify(error="forbidden"), 403
    d = request.get_json(silent=True) or {}
    con = db()
    rr = con.execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
    results = json.loads(rr["data"]) if rr else {}
    items, seen = [], set()
    legacy = get_legacy(cid)
    for user in sorted(results):
        if user in legacy and (legacy[user]["scores"] or legacy[user]["comments"]):
            continue  # внесок уже перевірено в попередній версії
        for pp in ((results[user] or {}).get("perProject") or {}).values():
            for it in (pp.get("list") or []):
                k = (user, it.get("title", ""))
                if k[1] and k not in seen:
                    seen.add(k)
                    items.append(k)
    if not items:
        return jsonify(error="no_results"), 400
    pool = [r["login"] for r in con.execute(
        "SELECT login FROM users WHERE role='jury' ORDER BY login")]
    if not pool:
        return jsonify(error="no_jury"), 400
    try:
        per = max(1, min(int(d.get("per_article") or 1), len(pool)))
    except (TypeError, ValueError):
        per = 1
    if d.get("reset"):
        con.execute("DELETE FROM article_assignments WHERE contest_id=?", (cid,))
    load = {j: 0 for j in pool}
    have = {}
    existing = con.execute("SELECT participant, article, jury FROM article_assignments "
                           "WHERE contest_id=?", (cid,)).fetchall()
    for r in existing:
        k = (r["participant"], r["article"])
        if k in seen and r["jury"] in load:
            have.setdefault(k, set()).add(r["jury"])
            load[r["jury"]] += 1
        else:  # застаріле призначення: статті вже немає або журі видалено
            con.execute("DELETE FROM article_assignments WHERE contest_id=? AND participant=? "
                        "AND article=? AND jury=?", (cid, r["participant"], r["article"], r["jury"]))
    rnd = random.Random(cid)
    rnd.shuffle(items)
    for k in items:
        got = have.setdefault(k, set())
        while len(got) < per:
            cand = [j for j in pool if j not in got]
            j = min(cand, key=lambda x: (load[x], rnd.random()))
            con.execute("INSERT OR IGNORE INTO article_assignments VALUES(?,?,?,?)",
                        (cid, k[0], k[1], j))
            got.add(j)
            load[j] += 1
    con.commit()
    return jsonify(ok=True, items=len(items), load=load)

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
    legacy = get_legacy(cid)
    hosts = []
    for p in contest["projects"]:
        m = re.match(r"https?://([^/]+)", p)
        if m:
            hosts.append(m.group(1))

    rows = []
    for user in contest["participants"]:
        r = results.get(user)
        a = assess.get(user, {})
        quality, n = article_quality(a, legacy.get(user))
        sp = split_stats(r)
        score = eval_formula(contest["formula"],
                             {"bytes": r["bytes"], "edits": r["edits"],
                              "articles": r["articles"], "quality": quality,
                              "created": sp["created"], "expanded": sp["expanded"]}) if r else None
        all_comments = [f"[{t['legacy_title']}] {c['jury']} ({c['date']}): {c['text']}"
                        for c in (legacy.get(user) or {}).get("comments", [])]
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
        head += [t["th_b"]] + hosts + [t["th_e"], t["th_split"], t["th_split_b"]]
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
            sp = split_stats(r) if r else None
            line.append(f"{sp['created']} / {sp['expanded']}" if sp else "")
            line.append(f"{sp['bc']} / {sp['be']}" if sp else "")
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
