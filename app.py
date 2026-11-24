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
CREATE TABLE IF NOT EXISTS contest_jury(
  contest_id TEXT, login TEXT, PRIMARY KEY(contest_id, login));
CREATE TABLE IF NOT EXISTS patrol_results(
  contest_id TEXT PRIMARY KEY, data TEXT, updated INTEGER);
"""

# раніше мінімальний внесок учасника був зашитий (1000 Б) для всіх конкурсів;
# тепер це налаштування конкурсу, а значення нижче лише зберігає поведінку вже наявних конкурсів
LEGACY_MIN_BYTES = 1000

def init_db():
    con = sqlite3.connect(DB_PATH)
    had_reviews = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_reviews'").fetchone()
    had_cj = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contest_jury'").fetchone()
    con.executescript(SCHEMA)
    if not had_reviews:
        # раніше «перевірено» = є оцінка; переносимо це в явні відмітки
        con.execute("INSERT OR IGNORE INTO article_reviews "
                    "SELECT contest_id, participant, article, jury FROM article_scores")
    cols = [r[1] for r in con.execute("PRAGMA table_info(contests)")]
    if "tpl_talk_only" not in cols:
        con.execute("ALTER TABLE contests ADD COLUMN tpl_talk_only INTEGER DEFAULT 0")
    if "review_scope" not in cols:
        con.execute("ALTER TABLE contests ADD COLUMN review_scope TEXT DEFAULT 'all'")
    for _col, _ddl in (("opt_refs", "INTEGER DEFAULT 0"), ("opt_images", "INTEGER DEFAULT 0"),
                       ("opt_status", "INTEGER DEFAULT 0"), ("cat_featured", "TEXT DEFAULT ''"),
                       ("cat_good", "TEXT DEFAULT ''"), ("cat_list", "TEXT DEFAULT ''"), ("src_tag", "TEXT DEFAULT ''"), ("min_expand", "INTEGER DEFAULT 0"), ("scope_refs", "INTEGER DEFAULT 0"),
                       ("scope_images", "INTEGER DEFAULT 0"), ("patrol", "INTEGER DEFAULT 0"),
                       ("patrol_end", "TEXT DEFAULT ''")):
        if _col not in cols:
            con.execute(f"ALTER TABLE contests ADD COLUMN {_col} {_ddl}")
    if "min_bytes" not in cols:
        # мінімальний внесок учасника (сума доданих байтів); 0 = без обмеження
        con.execute("ALTER TABLE contests ADD COLUMN min_bytes INTEGER DEFAULT 0")
        # наявні конкурси мали поріг 1000 Б — зберігаємо їхню поведінку, адмін може змінити
        con.execute("UPDATE contests SET min_bytes=?", (LEGACY_MIN_BYTES,))
    if "min_created" not in cols:
        # мінімум байтів для нової (започаткованої) статті (0 = без обмеження)
        con.execute("ALTER TABLE contests ADD COLUMN min_created INTEGER DEFAULT 0")
    if not had_cj:
        # раніше журі було спільним для всіх конкурсів: щоб не порушити перевірки, що вже тривають,
        # додаємо всіх наявних журі до всіх наявних конкурсів (адмін зможе звузити склад)
        con.execute("INSERT OR IGNORE INTO contest_jury "
                    "SELECT c.id, u.login FROM contests c, users u WHERE u.role='jury'")
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
FORMULA_VARS = ("bytes", "edits", "articles", "quality", "created", "expanded",
                "refs", "images", "good", "featured", "flist")

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
T["uk"].update(my_progress_title="Мій прогрес")
T["en"].update(my_progress_title="My progress")
T["pl"].update(my_progress_title="Mój postęp")
T["uk"].update(review_scope="Що перевіряє журі", scope_all="Усі статті", scope_created="Лише нові статті",
               scope_expanded="Лише доповнення", scope_label="Перевірка:",
               scope_h="Обмежує якісну перевірку: розподіл, перелік статей, прогрес і якість враховують лише обрані статті. Кількісний внесок не змінюється.")
T["en"].update(review_scope="What the jury reviews", scope_all="All articles", scope_created="New articles only",
               scope_expanded="Expansions only", scope_label="Review:",
               scope_h="Limits the qualitative review: distribution, article list, progress and quality use only the chosen articles. Quantitative contribution is unchanged.")
T["pl"].update(review_scope="Co sprawdza jury", scope_all="Wszystkie artykuły", scope_created="Tylko nowe artykuły",
               scope_expanded="Tylko rozbudowy", scope_label="Sprawdzanie:",
               scope_h="Ogranicza ocenę jakościową: podział, lista artykułów, postęp i jakość uwzględniają tylko wybrane artykuły. Wkład ilościowy się nie zmienia.")
T["uk"].update(hidden_min="Приховано учасників із внеском менше мінімального порогу")
T["en"].update(hidden_min="Participants hidden for contributing less than the minimum")
T["pl"].update(hidden_min="Ukryci uczestnicy z wkładem poniżej minimalnego progu")
T["pl"].update(legacy_title="Sprawdzono przed aktualizacją",
               legacy_h="Ten wkład oceniono w poprzedniej wersji: ocena liczy się do jakości, a artykuły nie są rozdzielane.")

T["uk"].update(hist_date="Дата", hist_user="Користувач", hist_size="Байти", hist_comment="Опис редагування")
T["en"].update(hist_date="Date", hist_user="User", hist_size="Bytes", hist_comment="Edit summary")
T["pl"].update(hist_date="Data", hist_user="Użytkownik", hist_size="Bajty", hist_comment="Opis edycji")

_NEW = {
 "uk": dict(
   th_created_n="Створено", th_expanded_n="Доповнено",
   formula="Формула ранжування для журі (необов'язково — залиште порожнім, якщо не потрібна)",
   parts_saved="Список учасників збережено", parts_not_saved="НЕ збережено — натисніть «Зберегти»", parts_save_hint="Щоб список зберігся, натисніть «Створити»",
   reassign_title="Перепризначити неперевірені статті", reassign_h="Передає лише ті неперевірені статті, що призначені вибраному журі, іншому члену журі. Перевірені статті, оцінки та коментарі лишаються без змін.", reassign_from="Від кого", reassign_to="Кому", reassign_btn="Перепризначити", tip_reassign="Передати неперевірені статті одного журі іншому. Перевірені, оцінки й коментарі не змінюються", reassign_pick="Оберіть обох членів журі", reassign_same="Оберіть різних членів журі", reassign_none="Немає неперевірених статей для передачі", reassign_confirm="Передати неперевірених статей: {n}? Перевірені статті ({k}) залишаться у попереднього журі, оцінки й коментарі не змінюються.", reassign_done="Передано статей",
   contest_jury="Журі цього конкурсу",
   contest_jury_h="Лише ці члени журі бачитимуть конкурс і отримуватимуть статті на перевірку. Уже виставлені оцінки та позначки не видаляються.",
   no_jury_accounts="Спершу створіть облікові записи журі у вкладці «Журі та адміни».",
   tpl_replace="Замінити поточний список учасників (а не додавати до нього)",
   save_update="Зберегти й оновити дані",
   upd_h="Змінили дати, шаблон чи учасників? «Зберегти й оновити дані» збереже зміни в цьому ж конкурсі (оцінки й коментарі лишаються) і одразу перерахує внесок.",
   tip_th_u="Вікі-акаунт учасника",
   tip_th_b="Сума доданих байтів за період конкурсу",
   tip_th_e="Кількість редагувань / кількість унікальних сторінок",
   tip_th_split="Кількість створених / доповнених статей",
   tip_th_created_n="Скільки нових статей створив учасник",
   tip_th_expanded_n="Скільки наявних статей учасник доповнив",
   tip_th_avg="Середня оцінка журі: спершу по кожній статті, потім по всіх статтях",
   tip_th_score="Бал за формулою конкурсу",
   tip_th_progress="Скільки статей перевірено (для журі — з призначених вам)",
   tip_user_link="Відкрити внесок учасника у вікі",
   tip_quant="Кількісний внесок: байти, редагування, статті",
   tip_qual="Якісний аналіз: оцінки журі, прогрес перевірки, список статей",
   tip_export="Завантажити таблицю у форматі CSV",
   tip_check="Завантажити з вікі актуальний внесок учасників за період конкурсу",
   tip_distribute="Розподілити статті між журі цього конкурсу",
   tip_per_article="Скільком членам журі призначати кожну статтю",
   tip_reset="Скасувати поточні призначення й розподілити з нуля (оцінки та коментарі не видаляються)",
   tip_articles_btn="Показати статті учасника",
   tip_af_all="Усі статті учасника", tip_af_created="Лише створені статті", tip_af_expanded="Лише доповнені статті",
   tip_rf_all="Без фільтра за статусом перевірки", tip_rf_todo="Статті, яких ви ще не перевірили",
   tip_rf_done="Статті, які ви вже перевірили", tip_rf_mine="Статті, призначені вам",
   tip_card="Відкрити статтю для перевірки",
   tip_prev="Попередня стаття у списку", tip_next="Наступна стаття у списку", tip_close="Закрити (Esc)",
   tip_status="Ви ще не перевірили цю статтю",
   tip_unrev="Натисніть, щоб зняти позначку «перевірено»",
   tip_rev_next="Позначити статтю перевіреною й відкрити наступну",
   tip_next_btn="Відкрити наступну статтю",
   tip_my_score="Ваша оцінка статті (число). Виставлена оцінка автоматично позначає статтю перевіреною",
   tip_comment="Додати коментар до статті",
   tip_tab_article="Показати статтю у вікні", tip_tab_history="Історія редагувань статті",
   tip_tab_preview="Короткий опис і зображення зі статті",
   tip_tab_c="Створення й редагування конкурсів", tip_tab_u="Облікові записи журі та адмінів",
   tip_tab_p="Зміна вашого пароля",
   tip_c_name="Назва конкурсу, яку бачать адміни та журі",
   tip_projects="Вікі-проєкти, у яких рахується внесок, по одному посиланню в рядку",
   tip_dstart="Редагування раніше цієї дати не враховуються",
   tip_dend="Редагування пізніше цієї дати не враховуються",
   tip_tpl="Посилання на шаблон, яким позначено статті конкурсу. Внесок рахується лише в цих статтях",
   tip_talk="Увімкніть, якщо шаблон стоїть на сторінках обговорення статей",
   tip_tpl_btn="Знайти статті з шаблоном і зібрати авторів їхніх редагувань за вказаний період",
   tip_replace="Увімкнено: список учасників буде замінено знайденими. Вимкнено: знайдені додадуться до поточного списку",
   tip_parts="Імена вікі-акаунтів учасників, по одному в рядку",
   tip_scope="Які статті перевіряє журі: усі, лише нові чи лише доповнення",
   tip_formula="Необов'язково. Порожня формула = не використовується. Змінні: bytes, edits, articles, quality, created, expanded, refs, images, good, featured",
   tip_cjury="Хто з журі працює над цим конкурсом",
   tip_save="Зберегти зміни конкурсу",
   tip_save_update="Зберегти зміни та одразу заново перерахувати внесок за новими параметрами",
   tip_edit="Змінити параметри конкурсу без створення нового",
   tip_delete="Видалити конкурс разом з усіма оцінками й коментарями",
   tip_role="Адмін налаштовує систему, журі лише оцінює статті",
   tip_uname="Ім'я, яке буде видно в коментарях",
   tip_ulogin="Логін для входу; для входу через MediaWiki — ім'я вікі-акаунта",
   tip_upass="Пароль для входу",
   tip_create_u="Створити обліковий запис",
   tip_del_user="Видалити обліковий запис"),
 "en": dict(
   th_created_n="Created", th_expanded_n="Expanded",
   formula="Ranking formula for the jury (optional — leave empty if not needed)",
   parts_saved="Participant list saved", parts_not_saved="NOT saved — click “Save”", parts_save_hint="Click “Create” to keep this list",
   reassign_title="Reassign unreviewed articles", reassign_h="Moves only the unreviewed articles assigned to the selected juror to another juror. Reviewed articles, scores and comments stay unchanged.", reassign_from="From", reassign_to="To", reassign_btn="Reassign", tip_reassign="Move unreviewed articles from one juror to another. Reviewed articles, scores and comments are not changed", reassign_pick="Choose both jurors", reassign_same="Choose different jurors", reassign_none="No unreviewed articles to move", reassign_confirm="Move {n} unreviewed articles? Reviewed articles ({k}) stay with the previous juror; scores and comments are not changed.", reassign_done="Articles moved",
   contest_jury="Jury of this contest",
   contest_jury_h="Only these jury members will see the contest and receive articles to review. Existing scores and marks are not deleted.",
   no_jury_accounts="First create jury accounts in the “Jury & admins” tab.",
   tpl_replace="Replace the current participant list (instead of adding to it)",
   save_update="Save and refresh data",
   upd_h="Changed dates, template or participants? “Save and refresh data” saves the changes in this same contest (scores and comments are kept) and recalculates contributions right away.",
   tip_th_u="Participant's wiki account",
   tip_th_b="Sum of added bytes during the contest period",
   tip_th_e="Number of edits / number of unique pages",
   tip_th_split="Number of created / expanded articles",
   tip_th_created_n="How many new articles the participant created",
   tip_th_expanded_n="How many existing articles the participant expanded",
   tip_th_avg="Average jury score: first per article, then across articles",
   tip_th_score="Score by the contest formula",
   tip_th_progress="How many articles are reviewed (for jury — of those assigned to you)",
   tip_user_link="Open the participant's contributions on the wiki",
   tip_quant="Quantitative contribution: bytes, edits, articles",
   tip_qual="Qualitative analysis: jury scores, review progress, article list",
   tip_export="Download the table as CSV",
   tip_check="Fetch up-to-date contributions of participants from the wiki for the contest period",
   tip_distribute="Distribute articles among the jury of this contest",
   tip_per_article="How many jurors each article is assigned to",
   tip_reset="Drop current assignments and distribute from scratch (scores and comments are not deleted)",
   tip_articles_btn="Show the participant's articles",
   tip_af_all="All articles of the participant", tip_af_created="Created articles only", tip_af_expanded="Expanded articles only",
   tip_rf_all="No filter by review status", tip_rf_todo="Articles you have not reviewed yet",
   tip_rf_done="Articles you have already reviewed", tip_rf_mine="Articles assigned to you",
   tip_card="Open the article for review",
   tip_prev="Previous article in the list", tip_next="Next article in the list", tip_close="Close (Esc)",
   tip_status="You have not reviewed this article yet",
   tip_unrev="Click to remove the “reviewed” mark",
   tip_rev_next="Mark the article as reviewed and open the next one",
   tip_next_btn="Open the next article",
   tip_my_score="Your score for the article (number). Giving a score automatically marks the article as reviewed",
   tip_comment="Add a comment to the article",
   tip_tab_article="Show the article in the frame", tip_tab_history="Edit history of the article",
   tip_tab_preview="Short extract and image from the article",
   tip_tab_c="Create and edit contests", tip_tab_u="Jury and admin accounts",
   tip_tab_p="Change your password",
   tip_c_name="Contest name shown to admins and jury",
   tip_projects="Wiki projects where contributions are counted, one link per line",
   tip_dstart="Edits before this date are not counted",
   tip_dend="Edits after this date are not counted",
   tip_tpl="Link to the template that marks contest articles. Contributions are counted only in these articles",
   tip_talk="Enable if the template is placed on article talk pages",
   tip_tpl_btn="Find articles with the template and collect authors of their edits within the given period",
   tip_replace="On: the participant list is replaced with the found users. Off: found users are added to the current list",
   tip_parts="Wiki usernames of participants, one per line",
   tip_scope="Which articles the jury reviews: all, only new or only expansions",
   tip_formula="Optional. Empty formula = not used. Variables: bytes, edits, articles, quality, created, expanded, refs, images, good, featured",
   tip_cjury="Which jury members work on this contest",
   tip_save="Save contest changes",
   tip_save_update="Save changes and immediately recalculate contributions with the new parameters",
   tip_edit="Change contest parameters without creating a new one",
   tip_delete="Delete the contest together with all its scores and comments",
   tip_role="Admins configure the system, jury only assess articles",
   tip_uname="Name shown in comments",
   tip_ulogin="Login name; for MediaWiki login — the wiki username",
   tip_upass="Password for signing in",
   tip_create_u="Create the account",
   tip_del_user="Delete the account"),
 "pl": dict(
   th_created_n="Utworzone", th_expanded_n="Rozbudowane",
   formula="Formuła rankingowa dla jury (opcjonalnie — zostaw puste, jeśli niepotrzebna)",
   parts_saved="Lista uczestników zapisana", parts_not_saved="NIE zapisano — kliknij „Zapisz”", parts_save_hint="Kliknij „Utwórz”, aby zachować listę",
   reassign_title="Przydziel ponownie niesprawdzone artykuły", reassign_h="Przenosi tylko niesprawdzone artykuły wybranego jurora do innego jurora. Sprawdzone artykuły, oceny i komentarze zostają bez zmian.", reassign_from="Od kogo", reassign_to="Komu", reassign_btn="Przydziel ponownie", tip_reassign="Przenieś niesprawdzone artykuły jednego jurora do innego. Sprawdzone, oceny i komentarze nie są zmieniane", reassign_pick="Wybierz obu jurorów", reassign_same="Wybierz różnych jurorów", reassign_none="Brak niesprawdzonych artykułów do przeniesienia", reassign_confirm="Przenieść niesprawdzone artykuły: {n}? Sprawdzone artykuły ({k}) zostają u poprzedniego jurora; oceny i komentarze nie są zmieniane.", reassign_done="Przeniesiono artykułów",
   contest_jury="Jury tego konkursu",
   contest_jury_h="Tylko ci członkowie jury zobaczą konkurs i dostaną artykuły do sprawdzenia. Wystawione oceny i oznaczenia nie są usuwane.",
   no_jury_accounts="Najpierw utwórz konta jury w zakładce „Jury i admini”.",
   tpl_replace="Zastąp bieżącą listę uczestników (zamiast dodawać do niej)",
   save_update="Zapisz i odśwież dane",
   upd_h="Zmieniłeś daty, szablon lub uczestników? „Zapisz i odśwież dane” zapisze zmiany w tym samym konkursie (oceny i komentarze zostają) i od razu przeliczy wkład.",
   tip_th_u="Konto wiki uczestnika",
   tip_th_b="Suma dodanych bajtów w okresie konkursu",
   tip_th_e="Liczba edycji / liczba unikalnych stron",
   tip_th_split="Liczba utworzonych / rozbudowanych artykułów",
   tip_th_created_n="Ile nowych artykułów utworzył uczestnik",
   tip_th_expanded_n="Ile istniejących artykułów rozbudował uczestnik",
   tip_th_avg="Średnia ocena jury: najpierw dla każdego artykułu, potem ze wszystkich artykułów",
   tip_th_score="Wynik wg formuły konkursu",
   tip_th_progress="Ile artykułów sprawdzono (dla jury — z przydzielonych tobie)",
   tip_user_link="Otwórz wkład uczestnika w wiki",
   tip_quant="Wkład ilościowy: bajty, edycje, artykuły",
   tip_qual="Analiza jakościowa: oceny jury, postęp sprawdzania, lista artykułów",
   tip_export="Pobierz tabelę jako CSV",
   tip_check="Pobierz z wiki aktualny wkład uczestników z okresu konkursu",
   tip_distribute="Rozdziel artykuły między jury tego konkursu",
   tip_per_article="Ilu jurorom przydzielić każdy artykuł",
   tip_reset="Usuń bieżące przydziały i rozdziel od nowa (oceny i komentarze nie są usuwane)",
   tip_articles_btn="Pokaż artykuły uczestnika",
   tip_af_all="Wszystkie artykuły uczestnika", tip_af_created="Tylko utworzone artykuły", tip_af_expanded="Tylko rozbudowane artykuły",
   tip_rf_all="Bez filtra statusu sprawdzenia", tip_rf_todo="Artykuły, których jeszcze nie sprawdziłeś",
   tip_rf_done="Artykuły, które już sprawdziłeś", tip_rf_mine="Artykuły przydzielone tobie",
   tip_card="Otwórz artykuł do sprawdzenia",
   tip_prev="Poprzedni artykuł na liście", tip_next="Następny artykuł na liście", tip_close="Zamknij (Esc)",
   tip_status="Nie sprawdziłeś jeszcze tego artykułu",
   tip_unrev="Kliknij, aby usunąć oznaczenie „sprawdzono”",
   tip_rev_next="Oznacz artykuł jako sprawdzony i otwórz następny",
   tip_next_btn="Otwórz następny artykuł",
   tip_my_score="Twoja ocena artykułu (liczba). Wystawienie oceny automatycznie oznacza artykuł jako sprawdzony",
   tip_comment="Dodaj komentarz do artykułu",
   tip_tab_article="Pokaż artykuł w oknie", tip_tab_history="Historia edycji artykułu",
   tip_tab_preview="Krótki opis i obraz z artykułu",
   tip_tab_c="Tworzenie i edycja konkursów", tip_tab_u="Konta jury i adminów",
   tip_tab_p="Zmiana twojego hasła",
   tip_c_name="Nazwa konkursu widoczna dla adminów i jury",
   tip_projects="Projekty wiki, w których liczy się wkład, jeden link w wierszu",
   tip_dstart="Edycje sprzed tej daty nie są liczone",
   tip_dend="Edycje po tej dacie nie są liczone",
   tip_tpl="Link do szablonu oznaczającego artykuły konkursu. Wkład liczy się tylko w tych artykułach",
   tip_talk="Włącz, jeśli szablon jest na stronach dyskusji artykułów",
   tip_tpl_btn="Znajdź artykuły z szablonem i zbierz autorów ich edycji z podanego okresu",
   tip_replace="Włączone: lista uczestników zostanie zastąpiona znalezionymi. Wyłączone: znalezieni zostaną dodani do bieżącej listy",
   tip_parts="Nazwy kont wiki uczestników, jedna w wierszu",
   tip_scope="Które artykuły sprawdza jury: wszystkie, tylko nowe lub tylko rozbudowy",
   tip_formula="Opcjonalnie. Pusta formuła = nieużywana. Zmienne: bytes, edits, articles, quality, created, expanded, refs, images, good, featured",
   tip_cjury="Którzy członkowie jury pracują przy tym konkursie",
   tip_save="Zapisz zmiany konkursu",
   tip_save_update="Zapisz zmiany i od razu przelicz wkład według nowych parametrów",
   tip_edit="Zmień parametry konkursu bez tworzenia nowego",
   tip_delete="Usuń konkurs razem ze wszystkimi ocenami i komentarzami",
   tip_role="Admin konfiguruje system, jury tylko ocenia artykuły",
   tip_uname="Imię widoczne w komentarzach",
   tip_ulogin="Login do logowania; dla logowania przez MediaWiki — nazwa konta wiki",
   tip_upass="Hasło do logowania",
   tip_create_u="Utwórz konto",
   tip_del_user="Usuń konto"),
}
for _l, _d in _NEW.items():
    T[_l].update(_d)

_NEW2 = {'uk': {'th_refs': 'Джерела', 'th_images': 'Зображення', 'th_status': 'Вибр. / добрі / списки', 'tip_th_refs': 'Кількість доданих джерел (виносок): лише в правках із потрібним хештегом (якщо він заданий), інакше в усіх правках учасника', 'tip_th_images': 'Скільки зображень учасник додав своїми правками в період кампанії: кожну правку порівнюємо з її попередньою версією', 'tip_th_status': 'Скільки статей отримали статус вибраної / доброї / вибраного списку саме в межах кампанії', 'chip_refs': 'Джерела', 'chip_images': 'Зображення', 'tip_chip_refs': 'Додано джерел (виносок) правками цього учасника в цій статті', 'tip_chip_images': 'Зображень, доданих правками цього учасника в цій статті', 'status_featured': 'Вибрана', 'status_good': 'Добра', 'status_title': 'Добрі та вибрані статті кампанії', 'status_h': 'Лише статті, яким статус надано (за шаблоном ВЗВ на сторінці обговорення) у межах дат кампанії. Для вибраних списків використовується порівняння сторінки зі списком.', 'status_nocat': 'Вкажіть категорії вибраних і добрих статей у налаштуваннях конкурсу, потім запустіть «Перевірити внесок».', 'status_none': 'У межах кампанії таких статей не знайдено.', 'tip_status_box': 'Статті, що стали вибраними, добрими чи вибраними списками під час кампанії', 'th_article': 'Стаття', 'th_status_kind': 'Статус', 'th_date': 'Дата', 'th_authors': 'Учасники', 'mode_status': 'Статуси', 'mode_patrol': 'Патрулювання', 'tip_mode_status': 'Показати статті, що стали добрими чи вибраними в межах кампанії', 'tip_mode_patrol': 'Показати стан патрулювання статей кампанії', 'extras_running': 'Джерела, зображення, статуси…', 'no_extras': 'Спершу запустіть «Перевірити внесок» з увімкненими показниками джерел/зображень.', 'scope_refs': 'Лише статті з доданими джерелами', 'scope_images': 'Лише статті з доданими зображеннями', 'tip_scope_refs': 'Журі бачить і оцінює тільки статті, де учасник додав джерела', 'tip_scope_images': 'Журі бачить і оцінює тільки статті, де учасник додав зображення', 'scope_extra_h': 'Потребує показників нижче; їх буде пораховано під час «Перевірити внесок». Після зміни заново розподіліть статті.', 'extras_title': 'Додаткові показники', 'opt_refs': 'Рахувати додані джерела', 'opt_images': 'Рахувати додані зображення', 'opt_status': 'Шукати добрі та вибрані статті (статус отримано в межах кампанії)', 'tip_opt_refs': 'Додає колонку й змінну refs у формулу. Рахує кількість доданих виносок <ref> і шаблонів sfn; з хештегом — лише в правках із ним', 'tip_opt_images': 'Додає колонку й змінну images у формулу. Рахує файли, галереї та зображення в картках, додані саме правками учасника', 'tip_opt_status': 'Добрі та вибрані статті береться з шаблону {{Повідомлення ВЗВ}} на сторінці обговорення. Додає вкладку «Статуси» та змінні featured, good і flist у формулу', 'cat_featured': 'Сторінка вибраних статей', 'cat_good': 'Сторінка добрих статей', 'tip_cat_featured': "Необов'язково: запасна сторінка зі списком вибраних статей (на випадок, якщо шаблона немає)", 'tip_cat_good': "Необов'язково: запасна сторінка зі списком добрих статей (на випадок, якщо шаблона немає)", 'cat_h': "Добрі та вибрані статті визначаються за шаблоном {{Повідомлення ВЗВ}} на сторінці обговорення: статус зараховується, якщо дата події «статус … надано» припадає на кампанію (лише українська вікіпедія). Для вибраних списків шаблона немає, тому використовується сторінка нижче (за замовчуванням «Вікіпедія:Вибрані списки»). Поля вибраних і добрих статей необов'язкові: це запасний спосіб за списками.", 'patrol_opt': 'Патрулювання', 'patrol_end': 'Кінець патрулювання (якщо пізніше за кінець кампанії)', 'tip_patrol_opt': 'Додає вкладку «Патрулювання»: які статті кампанії відпатрульовано і хто патрулював. Оцінка журі не потрібна', 'tip_patrol_end': 'Патрулювання після цієї дати не враховується. Порожньо — до кінця кампанії', 'patrol_opt_h': 'Дані беруться з журналу патрулювання вікі. Вкладка доступна лише адміну.', 'patrol_title': 'Патрулювання статей кампанії', 'patrol_h': 'Відпатрульовано = у журналі патрулювання є запис за період кампанії', 'patrol_deadline': 'до', 'patrol_run': 'Оновити дані патрулювання', 'patrol_running': 'Читаю журнал патрулювання…', 'tip_patrol_run': 'Прочитати журнал патрулювання для всіх статей кампанії (може тривати кілька хвилин)', 'tip_patrol_box': 'Стан патрулювання статей кампанії', 'patrol_nodata': 'Даних ще немає. Спершу «Перевірити внесок», потім «Оновити дані патрулювання».', 'patrol_patrolled': 'Відпатрульовано', 'patrol_left': 'Залишилось', 'patrol_total': 'Усього', 'patrol_unknown': 'не вдалося перевірити', 'patrol_updated': 'Оновлено', 'patrol_participants': 'Учасники кампанії, що патрулювали', 'patrol_outsiders': 'Сторонні патрульні', 'patrol_none': 'Немає записів.', 'patrol_own': 'Свої статті', 'patrol_others': 'Чужі статті', 'tip_patrol_part': 'Учасники кампанії: скільки статей патрулювали, з них власних (де вони автори внеску) і чужих', 'tip_patrol_out': 'Користувачі поза списком учасників, які патрулювали статті кампанії', 'patrol_unpatrolled': 'Непатрульовані статті', 'patrol_by_article': 'Хто патрулював кожну статтю', 'patrol_by': 'Патрульні', 'status_list': 'Вибраний список', 'cat_list': 'Сторінка вибраних списків', 'tip_cat_list': 'Сторінка проєкту зі списком вибраних списків. Для української вікіпедії за замовчуванням «Вікіпедія:Вибрані списки»', 'patrol_authors': 'Статті учасників кампанії', 'patrol_arts': 'Статей', 'patrol_auto_col': 'Автопатруль', 'patrol_by_out': 'Сторонні патрульні', 'patrol_auto_short': 'авто', 'tip_patrol_authors': 'Для кожного учасника кампанії: скільки його статей відпатрульовано і скільки лишилось', 'tip_patrol_auto': 'Статті, відпатрульовані автоматично (автопатрульованим учасником)', 'tip_patrol_by_out': 'Скільки статей учасника відпатрулював хтось зі сторонніх патрульних', 'src_tag': 'Хештег у описі правки (джерела)', 'tip_src_tag': "Обов'язковий хештег кампанії, напр. #1Lib1Ref. Якщо вказано, джерела рахуються лише в правках учасника з цим хештегом в описі", 'src_tag_h': 'Якщо вказано, аналізуються лише правки учасника з цим хештегом, і рахується кількість виносок, доданих саме ними (швидше й простіше). Якщо порожньо, аналізуються всі правки учасника.', 'min_expand': 'Мінімум байтів для доповнення статті', 'tip_min_expand': 'Доповнення статті (не створення) менше за це число доданих байтів не приймається: воно не потрапляє на перевірку журі й не рахується доповненим. 0 — без обмеження', 'min_expand_h': 'Стосується лише доповнених статей: нові статті не обмежуються. Оцінки, що вже виставлені, не видаляються. Після зміни статті варто розподілити заново («Розподілити між журі» без «Перерозподілити з нуля»).'}, 'en': {'th_refs': 'Sources', 'th_images': 'Images', 'th_status': 'Feat. / good / lists', 'tip_th_refs': "Number of sources (references) added: only in edits with the required hashtag (if set), otherwise in all of the participant's edits", 'tip_th_images': 'Images the participant added by their own edits during the campaign: each edit is compared with its previous version', 'tip_th_status': 'Articles that became featured / good / featured lists during the campaign', 'chip_refs': 'Sources', 'chip_images': 'Images', 'tip_chip_refs': "Sources (references) added by this participant's edits in this article", 'tip_chip_images': "Images added by this participant's edits in this article", 'status_featured': 'Featured', 'status_good': 'Good', 'status_title': 'Good and featured articles of the campaign', 'status_h': 'Only articles whose status was granted (per the ВЗВ template on the talk page) within the campaign dates. Featured lists use a comparison of the list page.', 'status_nocat': 'Set the featured / good article categories in the contest settings, then run “Check contributions”.', 'status_none': 'No such articles found within the campaign.', 'tip_status_box': 'Articles that became featured, good or featured lists during the campaign', 'th_article': 'Article', 'th_status_kind': 'Status', 'th_date': 'Date', 'th_authors': 'Participants', 'mode_status': 'Statuses', 'mode_patrol': 'Patrol', 'tip_mode_status': 'Show articles that became good or featured within the campaign', 'tip_mode_patrol': 'Show the patrol state of campaign articles', 'extras_running': 'Sources, images, statuses…', 'no_extras': 'Run “Check contributions” first with sources/images enabled.', 'scope_refs': 'Only articles with added sources', 'scope_images': 'Only articles with added images', 'tip_scope_refs': 'The jury sees and reviews only articles where the participant added sources', 'tip_scope_images': 'The jury sees and reviews only articles where the participant added images', 'scope_extra_h': 'Needs the indicators below; they are computed during “Check contributions”. Redistribute articles after changing this.', 'extras_title': 'Additional indicators', 'opt_refs': 'Count added sources', 'opt_images': 'Count added images', 'opt_status': 'Find good and featured articles (status gained within the campaign)', 'tip_opt_refs': 'Adds a column and the refs formula variable. Counts added <ref> tags and sfn templates; with a hashtag, only in edits that have it', 'tip_opt_images': "Adds a column and the images formula variable. Counts files, galleries and infobox images added by the participant's own edits", 'tip_opt_status': 'Good and featured articles are read from the {{Повідомлення ВЗВ}} template on the talk page. Adds the “Statuses” tab and the featured, good and flist formula variables', 'cat_featured': 'Featured articles page', 'cat_good': 'Good articles page', 'tip_cat_featured': 'Optional: fallback page listing featured articles (used if the template is missing)', 'tip_cat_good': 'Optional: fallback page listing good articles (used if the template is missing)', 'cat_h': 'Good and featured articles are detected by the {{Повідомлення ВЗВ}} template on the talk page: a status counts if the date of the “status … granted” event falls within the campaign (Ukrainian Wikipedia only). The template does not cover featured lists, so the page below is used for them (default “Вікіпедія:Вибрані списки”). The featured and good article fields are optional: a fallback based on lists.', 'patrol_opt': 'Patrol', 'patrol_end': 'Patrol end (if later than the campaign end)', 'tip_patrol_opt': 'Adds the “Patrol” tab: which campaign articles are patrolled and who patrolled them. No jury review needed', 'tip_patrol_end': 'Patrols after this date are not counted. Empty = until the campaign end', 'patrol_opt_h': 'Data comes from the wiki patrol log. The tab is visible to admins only.', 'patrol_title': 'Patrol of campaign articles', 'patrol_h': 'Patrolled = the patrol log has an entry during the campaign period', 'patrol_deadline': 'until', 'patrol_run': 'Refresh patrol data', 'patrol_running': 'Reading the patrol log…', 'tip_patrol_run': 'Read the patrol log for all campaign articles (may take a few minutes)', 'tip_patrol_box': 'Patrol state of campaign articles', 'patrol_nodata': 'No data yet. Run “Check contributions” first, then “Refresh patrol data”.', 'patrol_patrolled': 'Patrolled', 'patrol_left': 'Remaining', 'patrol_total': 'Total', 'patrol_unknown': 'could not be checked', 'patrol_updated': 'Updated', 'patrol_participants': 'Campaign participants who patrolled', 'patrol_outsiders': 'Outside patrollers', 'patrol_none': 'No entries.', 'patrol_own': 'Own articles', 'patrol_others': 'Others’ articles', 'tip_patrol_part': 'Campaign participants: how many articles they patrolled, of which their own (they are contribution authors) and others’', 'tip_patrol_out': 'Users outside the participant list who patrolled campaign articles', 'patrol_unpatrolled': 'Unpatrolled articles', 'patrol_by_article': 'Who patrolled each article', 'patrol_by': 'Patrollers', 'status_list': 'Featured list', 'cat_list': 'Featured lists page', 'tip_cat_list': 'Project page listing featured lists. For Ukrainian Wikipedia the default is “Вікіпедія:Вибрані списки”', 'patrol_authors': 'Articles of campaign participants', 'patrol_arts': 'Articles', 'patrol_auto_col': 'Autopatrol', 'patrol_by_out': 'Outside patrollers', 'patrol_auto_short': 'auto', 'tip_patrol_authors': 'For each participant: how many of their articles are patrolled and how many remain', 'tip_patrol_auto': 'Articles patrolled automatically (by an autopatrolled user)', 'tip_patrol_by_out': "How many of the participant's articles were patrolled by someone outside the campaign", 'src_tag': 'Edit summary hashtag (sources)', 'tip_src_tag': "Mandatory campaign hashtag, e.g. #1Lib1Ref. If set, sources are counted only in the participant's edits with this hashtag in the summary", 'src_tag_h': "If set, only the participant's edits with this hashtag are analysed, and the number of references added by them is counted (faster and simpler). If empty, all of the participant's edits are analysed.", 'min_expand': 'Minimum bytes for an expanded article', 'tip_min_expand': 'An expansion (not a creation) below this number of added bytes is not accepted: it does not go to jury review and is not counted as expanded. 0 = no limit', 'min_expand_h': 'Applies to expanded articles only; new articles are not limited. Scores already given are not deleted. After changing it, redistribute articles (without “Redistribute from scratch”).'}, 'pl': {'th_refs': 'Źródła', 'th_images': 'Obrazy', 'th_status': 'Wyr. / dobre / listy', 'tip_th_refs': 'Liczba dodanych źródeł (przypisów): tylko w edycjach z wymaganym hashtagiem (jeśli podany), inaczej we wszystkich edycjach uczestnika', 'tip_th_images': 'Obrazy dodane własnymi edycjami uczestnika w czasie kampanii: każda edycja jest porównywana z poprzednią wersją', 'tip_th_status': 'Artykuły, które uzyskały status wyróżnionego / dobrego / listy wyróżnionej w trakcie kampanii', 'chip_refs': 'Źródła', 'chip_images': 'Obrazy', 'tip_chip_refs': 'Źródła (przypisy) dodane edycjami tego uczestnika w tym artykule', 'tip_chip_images': 'Obrazy dodane edycjami tego uczestnika w tym artykule', 'status_featured': 'Wyróżniony', 'status_good': 'Dobry', 'status_title': 'Dobre i wyróżnione artykuły kampanii', 'status_h': 'Tylko artykuły, którym nadano status (wg szablonu ВЗВ na stronie dyskusji) w terminie kampanii. Listy wyróżnione korzystają z porównania strony z listą.', 'status_nocat': 'Podaj kategorie artykułów wyróżnionych i dobrych w ustawieniach konkursu, potem uruchom „Sprawdź wkład”.', 'status_none': 'Nie znaleziono takich artykułów w kampanii.', 'tip_status_box': 'Artykuły, które stały się wyróżnione, dobre lub listami wyróżnionymi w trakcie kampanii', 'th_article': 'Artykuł', 'th_status_kind': 'Status', 'th_date': 'Data', 'th_authors': 'Uczestnicy', 'mode_status': 'Statusy', 'mode_patrol': 'Patrol', 'tip_mode_status': 'Pokaż artykuły, które stały się dobre lub wyróżnione w kampanii', 'tip_mode_patrol': 'Pokaż stan patrolowania artykułów kampanii', 'extras_running': 'Źródła, obrazy, statusy…', 'no_extras': 'Najpierw uruchom „Sprawdź wkład” z włączonymi źródłami/obrazami.', 'scope_refs': 'Tylko artykuły z dodanymi źródłami', 'scope_images': 'Tylko artykuły z dodanymi obrazami', 'tip_scope_refs': 'Jury widzi i ocenia tylko artykuły, w których uczestnik dodał źródła', 'tip_scope_images': 'Jury widzi i ocenia tylko artykuły, w których uczestnik dodał obrazy', 'scope_extra_h': 'Wymaga wskaźników poniżej; są liczone podczas „Sprawdź wkład”. Po zmianie rozdziel artykuły ponownie.', 'extras_title': 'Dodatkowe wskaźniki', 'opt_refs': 'Licz dodane źródła', 'opt_images': 'Licz dodane obrazy', 'opt_status': 'Szukaj dobrych i wyróżnionych artykułów (status uzyskany w kampanii)', 'tip_opt_refs': 'Dodaje kolumnę i zmienną refs w formule. Liczy dodane znaczniki <ref> i szablony sfn; z hashtagiem tylko w edycjach z nim', 'tip_opt_images': 'Dodaje kolumnę i zmienną images w formule. Liczy pliki, galerie i obrazy w infoboksach dodane własnymi edycjami uczestnika', 'tip_opt_status': 'Artykuły dobre i wyróżnione są czytane z szablonu {{Повідомлення ВЗВ}} na stronie dyskusji. Dodaje zakładkę „Statusy” i zmienne featured, good i flist w formule', 'cat_featured': 'Strona artykułów wyróżnionych', 'cat_good': 'Strona artykułów dobrych', 'tip_cat_featured': 'Opcjonalnie: zapasowa strona z listą artykułów wyróżnionych (gdy brak szablonu)', 'tip_cat_good': 'Opcjonalnie: zapasowa strona z listą artykułów dobrych (gdy brak szablonu)', 'cat_h': 'Artykuły dobre i wyróżnione są wykrywane przez szablon {{Повідомлення ВЗВ}} na stronie dyskusji: status liczy się, jeśli data zdarzenia „status … nadano” mieści się w kampanii (tylko ukraińska Wikipedia). Szablon nie obejmuje list wyróżnionych, więc dla nich używana jest strona poniżej (domyślnie „Вікіпедія:Вибрані списки”). Pola artykułów wyróżnionych i dobrych są opcjonalne: zapasowy sposób oparty na listach.', 'patrol_opt': 'Patrol', 'patrol_end': 'Koniec patrolowania (jeśli później niż koniec kampanii)', 'tip_patrol_opt': 'Dodaje zakładkę „Patrol”: które artykuły kampanii są patrolowane i kto je patrolował. Bez oceny jury', 'tip_patrol_end': 'Patrole po tej dacie nie są liczone. Puste = do końca kampanii', 'patrol_opt_h': 'Dane pochodzą z rejestru patrolowania wiki. Zakładka widoczna tylko dla adminów.', 'patrol_title': 'Patrol artykułów kampanii', 'patrol_h': 'Patrolowany = w rejestrze patrolowania jest wpis z okresu kampanii', 'patrol_deadline': 'do', 'patrol_run': 'Odśwież dane patrolu', 'patrol_running': 'Czytam rejestr patrolowania…', 'tip_patrol_run': 'Odczytaj rejestr patrolowania dla wszystkich artykułów kampanii (może potrwać kilka minut)', 'tip_patrol_box': 'Stan patrolowania artykułów kampanii', 'patrol_nodata': 'Brak danych. Najpierw „Sprawdź wkład”, potem „Odśwież dane patrolu”.', 'patrol_patrolled': 'Spatrolowane', 'patrol_left': 'Pozostało', 'patrol_total': 'Razem', 'patrol_unknown': 'nie udało się sprawdzić', 'patrol_updated': 'Zaktualizowano', 'patrol_participants': 'Uczestnicy kampanii, którzy patrolowali', 'patrol_outsiders': 'Patrolujący spoza kampanii', 'patrol_none': 'Brak wpisów.', 'patrol_own': 'Własne artykuły', 'patrol_others': 'Cudze artykuły', 'tip_patrol_part': 'Uczestnicy kampanii: ile artykułów patrolowali, w tym własnych (są autorami wkładu) i cudzych', 'tip_patrol_out': 'Użytkownicy spoza listy uczestników, którzy patrolowali artykuły kampanii', 'patrol_unpatrolled': 'Niespatrolowane artykuły', 'patrol_by_article': 'Kto patrolował każdy artykuł', 'patrol_by': 'Patrolujący', 'status_list': 'Wyróżniona lista', 'cat_list': 'Strona list wyróżnionych', 'tip_cat_list': 'Strona projektu z listą list wyróżnionych. Dla ukraińskiej Wikipedii domyślnie „Вікіпедія:Вибрані списки”', 'patrol_authors': 'Artykuły uczestników kampanii', 'patrol_arts': 'Artykułów', 'patrol_auto_col': 'Autopatrol', 'patrol_by_out': 'Patrolujący spoza kampanii', 'patrol_auto_short': 'auto', 'tip_patrol_authors': 'Dla każdego uczestnika: ile jego artykułów jest spatrolowanych, a ile zostało', 'tip_patrol_auto': 'Artykuły spatrolowane automatycznie (przez użytkownika z autopatrolem)', 'tip_patrol_by_out': 'Ile artykułów uczestnika spatrolował ktoś spoza kampanii', 'src_tag': 'Hashtag w opisie edycji (źródła)', 'tip_src_tag': 'Obowiązkowy hashtag kampanii, np. #1Lib1Ref. Jeśli podany, źródła są liczone tylko w edycjach uczestnika z tym hashtagiem w opisie', 'src_tag_h': 'Jeśli podany, analizowane są tylko edycje uczestnika z tym hashtagiem i liczona jest liczba przypisów dodanych właśnie nimi (szybciej i prościej). Jeśli puste, analizowane są wszystkie edycje uczestnika.', 'min_expand': 'Minimum bajtów dla rozbudowy artykułu', 'tip_min_expand': 'Rozbudowa (nie utworzenie) poniżej tej liczby dodanych bajtów nie jest akceptowana: nie trafia do oceny jury i nie jest liczona jako rozbudowana. 0 = bez limitu', 'min_expand_h': 'Dotyczy tylko rozbudowanych artykułów; nowe nie są ograniczone. Wystawione oceny nie są usuwane. Po zmianie rozdziel artykuły ponownie (bez „Rozdziel od nowa”).'}}
for _l, _d in _NEW2.items():
    T[_l].update(_d)
T['uk']['lang_menu']='Мова інтерфейсу'
T['uk'].update(theme_auto='Тема: авто (як у системі)',theme_light='Тема: світла',theme_dark='Тема: темна')
T['en'].update(theme_auto='Theme: auto (system)',theme_light='Theme: light',theme_dark='Theme: dark')
T['pl'].update(theme_auto='Motyw: auto (systemowy)',theme_light='Motyw: jasny',theme_dark='Motyw: ciemny')
T['en']['lang_menu']='Interface language'
T['pl']['lang_menu']='Język interfejsu'
_RANK = {
 "uk": dict(
   min_expand="Мінімум байтів для доповненої статті",
   tip_min_expand="Доповнення, де учасник додав менше байтів, не потрапляють на перевірку журі. 0 = без обмеження",
   min_expand_h="Доповнення з меншим приростом не приймаються: вони не потрапляють на перевірку журі, у прогрес і якість та не рахуються доповненими (кількість і змінна expanded у формулі). Загальні байти учасника не змінюються; виставлені оцінки не видаляються. 0 = вимкнено.",
   min_expand_chip="Мін. доповнення:", bytes_unit="Б",
   min_bytes="Мінімальний внесок учасника (байтів)",
   tip_min_bytes="Учасники, які додали за період менше байтів, не потрапляють у рейтинг і на перевірку. 0 = без обмеження",
   min_bytes_h="Сума доданих байтів учасника. Менший внесок — учасника приховано з таблиці, розподілу й експорту. 0 = вимкнено.",
   min_bytes_chip="Мін. внесок:",
   min_created="Мінімум байтів для нової статті",
   tip_min_created="Нові (започатковані) статті, у яких менше байтів, не потрапляють на перевірку журі. 0 = без обмеження",
   min_created_h="Те саме для нових статей (за правилами, напр., 3500 Б): замала нова стаття не потрапляє на перевірку й не рахується створеною (кількість і змінна created у формулі). Рахуються байти, додані учасником. 0 = вимкнено.",
   min_created_chip="Мін. нова стаття:",
   hidden_exp="Приховано статей, менших за мінімум (нові + доповнення)",
   th_avg_c="Бал: нові", th_avg_e="Бал: доповнені",
   tip_th_avg_c="Середній бал журі за нові статті учасника. Клікніть, щоб відсортувати",
   tip_th_avg_e="Середній бал журі за доповнені статті учасника. Клікніть, щоб відсортувати",
   top_title="Найвищі бали журі", top_created="Нові статті", top_expanded="Доповнені статті",
   top_show="Показати", top_none="Оцінених статей ще немає",
   top_h="Рейтинг статей за середнім балом журі — окремо створені та окремо доповнені.",
   top_csv="Експорт CSV ↓", tip_top_csv="Завантажити повні рейтинги (усі оцінені статті, а не лише показані) у CSV",
   top_c_type="Тип", top_c_article="Стаття", top_c_score="Сер. бал", top_c_n="Оцінок", top_c_link="Посилання",
   me_warn="⚠ Після збереження з перевірки випаде оцінених статей: {n}. Оцінки лишаться в базі, але не враховуватимуться в якості й балах.",
   me_ok="Оцінених статей, що випадуть, немає.",
   me_confirm="Відсіяти оцінені статті ({n})? Їхні оцінки більше не враховуватимуться в якості й балах (у базі вони лишаться).",
   me_more="…та ще"),
 "en": dict(
   min_expand="Minimum bytes for an expanded article",
   tip_min_expand="Expansions where the participant added fewer bytes are not sent to the jury. 0 = no limit",
   min_expand_h="Expansions below the threshold are not accepted: they are dropped from jury review, progress and quality and are not counted as expanded (the count and the expanded formula variable). The participant's total bytes are unchanged; existing scores are not deleted. 0 = off.",
   min_expand_chip="Min. expansion:", bytes_unit="B",
   min_bytes="Minimum participant contribution (bytes)",
   tip_min_bytes="Participants who added fewer bytes in the period are left out of the ranking and review. 0 = no limit",
   min_bytes_h="The participant's total added bytes. Smaller contributions are hidden from the table, distribution and export. 0 = off.",
   min_bytes_chip="Min. contribution:",
   min_created="Minimum bytes for a new article",
   tip_min_created="New (started) articles with fewer bytes are not sent to the jury. 0 = no limit",
   min_created_h="The same for new articles (e.g. 3500 B per the rules): a too-small new article is not reviewed and is not counted as created (the count and the created formula variable). Counts the bytes added by the participant. 0 = off.",
   min_created_chip="Min. new article:",
   hidden_exp="Articles hidden for being below the minimum (new + expansions)",
   th_avg_c="Score: new", th_avg_e="Score: expanded",
   tip_th_avg_c="Average jury score for the participant's new articles. Click to sort",
   tip_th_avg_e="Average jury score for the participant's expanded articles. Click to sort",
   top_title="Top jury scores", top_created="New articles", top_expanded="Expanded articles",
   top_show="Show", top_none="No scored articles yet",
   top_h="Articles ranked by average jury score — new and expanded separately.",
   top_csv="Export CSV ↓", tip_top_csv="Download the full rankings (all scored articles, not only the shown ones) as CSV",
   top_c_type="Type", top_c_article="Article", top_c_score="Avg score", top_c_n="Scores", top_c_link="Link",
   me_warn="⚠ After saving, scored articles that will drop out of the review: {n}. The scores stay in the database but will not count toward quality and scores.",
   me_ok="No scored articles will drop out.",
   me_confirm="Drop scored articles ({n})? Their scores will stop counting toward quality and scores (they stay in the database).",
   me_more="…and more:"),
 "pl": dict(
   min_expand="Minimum bajtów dla rozbudowanego artykułu",
   tip_min_expand="Rozbudowy, w których uczestnik dodał mniej bajtów, nie trafiają do oceny jury. 0 = bez limitu",
   min_expand_h="Rozbudowy poniżej progu nie są akceptowane: wypadają z oceny jury, postępu i jakości oraz nie są liczone jako rozbudowane (liczba i zmienna expanded w formule). Łączne bajty uczestnika się nie zmieniają; wystawione oceny nie są usuwane. 0 = wyłączone.",
   min_expand_chip="Min. rozbudowa:", bytes_unit="B",
   min_bytes="Minimalny wkład uczestnika (bajty)",
   tip_min_bytes="Uczestnicy, którzy dodali mniej bajtów w okresie, nie trafiają do rankingu ani oceny. 0 = bez limitu",
   min_bytes_h="Suma dodanych bajtów uczestnika. Mniejszy wkład — uczestnik ukryty w tabeli, podziale i eksporcie. 0 = wyłączone.",
   min_bytes_chip="Min. wkład:",
   min_created="Minimum bajtów dla nowego artykułu",
   tip_min_created="Nowe artykuły z mniejszą liczbą bajtów nie trafiają do oceny jury. 0 = bez limitu",
   min_created_h="To samo dla nowych artykułów (np. 3500 B wg regulaminu): za mały nowy artykuł nie trafia do oceny i nie jest liczony jako utworzony (liczba i zmienna created w formule). Liczą się bajty dodane przez uczestnika. 0 = wyłączone.",
   min_created_chip="Min. nowy artykuł:",
   hidden_exp="Ukryte artykuły poniżej minimum (nowe + rozbudowy)",
   th_avg_c="Ocena: nowe", th_avg_e="Ocena: rozbudowane",
   tip_th_avg_c="Średnia ocena jury za nowe artykuły uczestnika. Kliknij, aby sortować",
   tip_th_avg_e="Średnia ocena jury za rozbudowane artykuły uczestnika. Kliknij, aby sortować",
   top_title="Najwyższe oceny jury", top_created="Nowe artykuły", top_expanded="Rozbudowane artykuły",
   top_show="Pokaż", top_none="Brak ocenionych artykułów",
   top_h="Artykuły według średniej oceny jury — osobno nowe i rozbudowane.",
   top_csv="Eksport CSV ↓", tip_top_csv="Pobierz pełne rankingi (wszystkie ocenione artykuły, nie tylko pokazane) jako CSV",
   top_c_type="Typ", top_c_article="Artykuł", top_c_score="Śr. ocena", top_c_n="Ocen", top_c_link="Link",
   me_warn="⚠ Po zapisaniu z oceny wypadną ocenione artykuły: {n}. Oceny zostaną w bazie, ale nie będą liczone w jakości i wynikach.",
   me_ok="Żadne ocenione artykuły nie wypadną.",
   me_confirm="Pominąć ocenione artykuły ({n})? Ich oceny przestaną się liczyć w jakości i wynikach (zostaną w bazie).",
   me_more="…i jeszcze:"),
}
for _l, _d in _RANK.items():
    T[_l].update(_d)


_A11Y = {
 'uk': dict(help_btn="Докладніше", contest_sel="Конкурс", a11y_actions="Дії", progress="Перебіг", dlg_frame="Стаття у вікні", sort_hint="Сортувати"),
 'en': dict(help_btn="More details", contest_sel="Contest", a11y_actions="Actions", progress="Progress", dlg_frame="Article in the window", sort_hint="Sort"),
 'pl': dict(help_btn="Więcej szczegółów", contest_sel="Konkurs", a11y_actions="Akcje", progress="Postęp", dlg_frame="Artykuł w oknie", sort_hint="Sortuj"),
}
for _l, _d in _A11Y.items():
    T[_l].update(_d)

import re as _re
from markupsafe import Markup as _Markup, escape as _esc
_help_n = [0]

def _help_btn(text):
    _help_n[0] += 1
    i = "hp%d" % _help_n[0]
    return _Markup('<span class="help"><button type="button" class="help-btn" aria-label="%s" aria-expanded="false" aria-describedby="%s">?</button>'
                   '<span role="tooltip" id="%s" class="help-pop" hidden>%s</span></span>') % (_esc(T[lang()]["help_btn"]), i, i, _esc(text))

@app.template_global()
def hint_help(text, style="", label=None):
    """Підказка: перше речення видно, повне пояснення — у спливаючому вікні за кнопкою «?»."""
    text = str(text)
    parts = _re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    st = ' style="%s"' % _esc(style) if style else ""
    if len(text) <= 120 or len(parts) < 2:
        return _Markup('<div class="hint"%s>%s</div>') % (_Markup(st), text)
    return _Markup('<div class="hint"%s>%s %s</div>') % (_Markup(st), parts[0], _help_btn(text))

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
/* Стиль за Wikimedia Codex (design tokens): https://doc.wikimedia.org/codex/latest/ */
:root{
 --cdx-base:#fff;--cdx-neutral:#f8f9fa;--cdx-interactive:#eaecf0;
 --cdx-text:#202122;--cdx-subtle:#54595d;--cdx-placeholder:#72777d;
 --cdx-border:#a2a9b1;--cdx-border-subtle:#c8ccd1;--cdx-border-int:#72777d;
 --cdx-prog:#36c;--cdx-prog-hover:#447ff5;--cdx-prog-active:#233566;--cdx-prog-subtle:#eaf3ff;
 --cdx-destr:#d33;--cdx-destr-text:#b32424;--cdx-ok-text:#0f6f5a;--cdx-destr-subtle:#fee7e6;--cdx-ok:#14866d;--cdx-ok-subtle:#dff2eb;
 --cdx-on-prog:#fff;--cdx-backdrop:rgba(255,255,255,.65);--cdx-warn:#ac6600;--cdx-warn-subtle:#fef6e7;
 color-scheme:light;--cdx-radius:2px;--cdx-shadow:0 2px 2px rgba(0,0,0,.2);
 --ink:var(--cdx-text);--ink2:var(--cdx-text);--mut:var(--cdx-subtle);--paper:var(--cdx-neutral);--card:var(--cdx-base);
 --line:var(--cdx-border-subtle);--acc:var(--cdx-prog);--acc-soft:var(--cdx-prog-subtle);--gold:var(--cdx-prog);
 --ok:var(--cdx-ok-text);--err:var(--cdx-destr-text);
 --grad-light:var(--cdx-neutral);--grad-dark:var(--cdx-prog);
 --mono:ui-monospace,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--cdx-neutral);color:var(--cdx-text);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Lato,Helvetica,Arial,sans-serif}
a{color:var(--cdx-prog);text-decoration:none}a:hover{text-decoration:underline}
input,select,textarea{font:inherit;color:var(--cdx-text);background:var(--cdx-base);border:1px solid var(--cdx-border-int);border-radius:var(--cdx-radius);padding:5px 8px;min-height:32px;width:100%}
input::placeholder,textarea::placeholder{color:var(--cdx-placeholder)}
input:focus,select:focus,textarea:focus{outline:1px solid transparent;border-color:var(--cdx-prog);box-shadow:inset 0 0 0 1px var(--cdx-prog)}
input[type=checkbox],input[type=radio]{width:18px;height:18px;min-height:0;margin:3px;flex:none;accent-color:var(--cdx-prog)}
button{font:inherit;font-weight:700;cursor:pointer;border:1px solid var(--cdx-border);border-radius:var(--cdx-radius);padding:5px 12px;min-height:32px;background:var(--cdx-neutral);color:var(--cdx-text)}
button:focus-visible,a:focus-visible{outline:1px solid transparent;box-shadow:0 0 0 1px var(--cdx-base),0 0 0 3px var(--cdx-prog)}
.btn-p{background:var(--cdx-prog);border-color:var(--cdx-prog);color:var(--cdx-on-prog)}
.btn-p:hover{background:var(--cdx-prog-hover);border-color:var(--cdx-prog-hover)}
.btn-p:active{background:var(--cdx-prog-active);border-color:var(--cdx-prog-active)}
.btn-p:disabled{background:var(--cdx-border-subtle);border-color:var(--cdx-border-subtle);color:var(--cdx-placeholder);cursor:default}
.btn-s{background:var(--cdx-neutral);border:1px solid var(--cdx-border);color:var(--cdx-text)}
.btn-s:hover{background:var(--cdx-base);border-color:var(--cdx-border-int)}
.btn-d{background:var(--cdx-neutral);border:1px solid var(--cdx-border);color:var(--cdx-destr-text)}
.btn-d:hover{background:var(--cdx-destr-subtle);border-color:var(--cdx-destr)}
.card{background:var(--cdx-base);border:1px solid var(--cdx-border-subtle);border-radius:var(--cdx-radius);padding:20px;margin-bottom:14px}
h2{font-size:1.17em}h1,h2,h3{color:var(--cdx-text);font-family:"Linux Libertine","Georgia","Times",serif;font-weight:400}
.lbl{display:block;font-size:14px;font-weight:700;color:var(--cdx-text);margin:10px 0 4px}
.hint{font-size:13px;color:var(--cdx-subtle)}.mono{font-family:var(--mono)}
.chip{font-size:12px;background:var(--cdx-neutral);color:var(--cdx-subtle);border:1px solid var(--cdx-border);border-radius:var(--cdx-radius);padding:1px 8px}
.tag-a{background:var(--cdx-prog-subtle);color:var(--cdx-prog);border-color:var(--cdx-prog)}.tag-j{background:var(--cdx-neutral);color:var(--cdx-subtle)}
table{width:100%;border-collapse:collapse}
th{font-size:13px;font-weight:700;color:var(--cdx-text);background:var(--cdx-interactive);text-align:left;
padding:8px 10px;border-bottom:1px solid var(--cdx-border-subtle);white-space:nowrap}
td{padding:10px;border-bottom:1px solid var(--cdx-interactive);vertical-align:top}
.bar{height:6px;background:var(--cdx-interactive);border-radius:var(--cdx-radius);min-width:70px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--cdx-prog)}
.pbar{height:8px;background:var(--cdx-interactive);border-radius:var(--cdx-radius);overflow:hidden;width:100%}
.pbar i{display:block;height:100%;background:var(--cdx-prog)}
a.wl{color:var(--cdx-prog);text-decoration:none;font-weight:700}
header{background:var(--cdx-base);color:var(--cdx-text);border-bottom:1px solid var(--cdx-border-subtle);padding:10px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header a{color:var(--cdx-prog);text-decoration:none}header a:hover{text-decoration:underline}header a.on{color:var(--cdx-text);font-weight:700}
.mode{display:inline-flex;border:1px solid var(--cdx-border);border-radius:var(--cdx-radius);overflow:hidden;background:var(--cdx-neutral)}
.mode button{border:none;border-radius:0;background:var(--cdx-neutral);color:var(--cdx-text);border-right:1px solid var(--cdx-border)}
.mode button:last-child{border-right:none}.mode button.on{background:var(--cdx-prog-subtle);color:var(--cdx-prog);font-weight:700}
main{max-width:1200px;margin:0 auto;padding:20px}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.thbtn{background:none;border:0;padding:2px 0;min-height:24px;font:inherit;font-weight:700;color:inherit;cursor:pointer;white-space:nowrap}
.thbtn:hover{text-decoration:underline}
.art-card:focus-visible,[role=button]:focus-visible{outline:2px solid var(--cdx-prog);outline-offset:2px}
.help{display:inline-block;vertical-align:baseline;margin-left:4px}
.help-btn{min-height:24px;min-width:24px;padding:0;border-radius:50%;font-size:13px;line-height:1;background:var(--cdx-prog-subtle);color:var(--cdx-prog);border-color:var(--cdx-prog)}
.help-pop{position:fixed;z-index:3000;width:min(360px,calc(100vw - 24px));padding:10px 12px;font-size:13px;font-weight:400;line-height:1.5;
 background:var(--cdx-base);color:var(--cdx-text);border:1px solid var(--cdx-border-int);border-radius:var(--cdx-radius);box-shadow:0 6px 18px rgba(0,0,0,.25);text-align:left}
.help-pop[hidden]{display:none}
@media (prefers-reduced-motion:no-preference){.pbar i{transition:width .3s}}

.langsw a{font-size:12px;font-weight:700;padding:4px 8px;border:1px solid var(--cdx-border);border-radius:var(--cdx-radius);margin-left:4px;background:var(--cdx-neutral)}
.langsw a:hover{text-decoration:none;background:var(--cdx-base)}
.langsw a.on{background:var(--cdx-prog-subtle);border-color:var(--cdx-prog);color:var(--cdx-prog)}
.themebtn{padding:4px 9px;min-height:30px;font-size:14px;line-height:1.2}
.langmenu{position:relative;display:inline-block}
.burger{display:none;align-items:center;gap:6px;font-size:12px;padding:4px 9px;min-height:30px}
.burger i{display:block;width:14px;height:2px;background:currentColor;box-shadow:0 -4px 0 currentColor,0 4px 0 currentColor}
@media(max-width:820px){
 .burger{display:inline-flex}
 .langmenu .langsw{display:none;position:absolute;right:0;top:calc(100% + 6px);z-index:50;flex-direction:column;gap:4px;padding:8px;background:var(--cdx-base);border:1px solid var(--cdx-border-subtle);border-radius:var(--cdx-radius);box-shadow:var(--cdx-shadow)}
 .langmenu.open .langsw{display:flex}
 .langmenu .langsw a{margin:0;text-align:center;min-width:64px;padding:8px 12px;font-size:13px}
}
.carousel-wrap{position:relative;margin:4px 0}
.carousel{display:flex;gap:12px;overflow-x:auto;padding:4px 2px 12px;scroll-snap-type:x proximity;scrollbar-width:thin}
.carousel::-webkit-scrollbar{height:6px}
.carousel::-webkit-scrollbar-thumb{background:var(--cdx-border-subtle);border-radius:3px}
.art-card{flex:0 0 150px;scroll-snap-align:start;cursor:pointer;border-radius:var(--cdx-radius);overflow:hidden;
background:var(--cdx-base);border:1px solid var(--cdx-border-subtle);transition:box-shadow .15s,border-color .15s}
.art-card:hover{border-color:var(--cdx-border-int);box-shadow:var(--cdx-shadow)}
.art-card .thumb{width:100%;height:96px;object-fit:cover;display:block;background:var(--cdx-interactive)}
.art-card .thumb-ph{width:100%;height:96px;background:var(--cdx-interactive);display:flex;align-items:center;
justify-content:center;color:var(--cdx-subtle);font-size:12px}
.art-card .body{padding:8px}
.art-title{font-size:12px;font-weight:700;line-height:1.3;max-height:2.6em;overflow:hidden;color:var(--cdx-text)}
.car-nav{position:absolute;top:34px;width:30px;height:30px;border-radius:50%;background:var(--cdx-prog);
color:var(--cdx-on-prog);display:flex;align-items:center;justify-content:center;z-index:5;
box-shadow:var(--cdx-shadow);font-size:16px;line-height:1}
.car-nav.prev{left:-8px}.car-nav.next{right:-8px}
.st-ok{background:var(--cdx-ok-subtle);color:var(--cdx-ok-text)}.st-todo{background:var(--cdx-warn-subtle);color:var(--cdx-warn)}
.md-back{position:fixed;inset:0;background:var(--cdx-backdrop);z-index:1000;display:flex;align-items:center;justify-content:center;padding:12px}
.md-box{background:var(--cdx-base);border:1px solid var(--cdx-border-subtle);border-radius:var(--cdx-radius);box-shadow:0 20px 48px rgba(0,0,0,.2);width:100%;max-width:1360px;height:94vh;display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,1fr);overflow:hidden}
.md-left{display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--cdx-border-subtle)}
.md-right{padding:16px 18px;overflow:auto}
.md-tabs{display:flex;gap:6px;padding:10px 12px;border-bottom:1px solid var(--cdx-border-subtle);align-items:center;flex-wrap:wrap;background:var(--cdx-neutral)}
.md-tabs button.on{background:var(--cdx-prog-subtle);border-color:var(--cdx-prog);color:var(--cdx-prog)}
.md-pane{flex:1;overflow:auto;padding:16px 18px}
.md-frame{flex:1;width:100%;border:0;min-height:0}
.md-top{display:flex;gap:8px;align-items:center;margin-bottom:10px}
@media(max-width:820px){.md-box{grid-template-columns:1fr;overflow:auto;height:96vh}.md-left{height:62vh}}
:root[data-theme=dark]{--cdx-base:#101418;--cdx-neutral:#1b1f23;--cdx-interactive:#27292d;--cdx-text:#eaecf0;--cdx-subtle:#a2a9b1;--cdx-placeholder:#8b9097;
 --cdx-border:#72777d;--cdx-border-subtle:#404244;--cdx-border-int:#a2a9b1;
 --cdx-prog:#88a3e8;--cdx-prog-hover:#adc3f5;--cdx-prog-active:#c8d8fb;--cdx-prog-subtle:#1c2a4a;--cdx-on-prog:#101418;
 --cdx-destr:#f78a82;--cdx-destr-text:#f78a82;--cdx-ok-text:#5ac1a1;--cdx-destr-subtle:#3b1c1b;--cdx-ok:#5ac1a1;--cdx-ok-subtle:#12302a;--cdx-warn:#e0a64a;--cdx-warn-subtle:#33270d;
 --cdx-shadow:0 2px 6px rgba(0,0,0,.6);color-scheme:dark;--cdx-backdrop:rgba(0,0,0,.6);}
@media(prefers-color-scheme:dark){:root:not([data-theme]){--cdx-base:#101418;--cdx-neutral:#1b1f23;--cdx-interactive:#27292d;--cdx-text:#eaecf0;--cdx-subtle:#a2a9b1;--cdx-placeholder:#8b9097;
 --cdx-border:#72777d;--cdx-border-subtle:#404244;--cdx-border-int:#a2a9b1;
 --cdx-prog:#88a3e8;--cdx-prog-hover:#adc3f5;--cdx-prog-active:#c8d8fb;--cdx-prog-subtle:#1c2a4a;--cdx-on-prog:#101418;
 --cdx-destr:#f78a82;--cdx-destr-text:#f78a82;--cdx-ok-text:#5ac1a1;--cdx-destr-subtle:#3b1c1b;--cdx-ok:#5ac1a1;--cdx-ok-subtle:#12302a;--cdx-warn:#e0a64a;--cdx-warn-subtle:#33270d;
 --cdx-shadow:0 2px 6px rgba(0,0,0,.6);color-scheme:dark;--cdx-backdrop:rgba(0,0,0,.6);}}
"""

LOGIN_HTML = """<!doctype html><html lang="{{l}}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{t.brand}}</title><style>""" + BASE_CSS + """</style>
<script>
(function(){try{const v=localStorage.getItem('wr_theme');if(v==='light'||v==='dark')document.documentElement.setAttribute('data-theme',v)}catch(e){}})();
function themeNow(){const v=document.documentElement.getAttribute('data-theme');return v==='light'||v==='dark'?v:'auto'}
function paintTheme(){const m=themeNow(),ic={auto:'◐',light:'☀',dark:'☾'};document.querySelectorAll('.themebtn').forEach(b=>{const L=(b.getAttribute('data-t')||'').split('|');const nm=L[['auto','light','dark'].indexOf(m)]||m;b.textContent=ic[m];b.title=nm;b.setAttribute('aria-label',nm)})}
function cycleTheme(){const o={auto:'light',light:'dark',dark:'auto'},n=o[themeNow()];if(n==='auto')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',n);try{if(n==='auto')localStorage.removeItem('wr_theme');else localStorage.setItem('wr_theme',n)}catch(e){}paintTheme()}
document.addEventListener('DOMContentLoaded',paintTheme);
function toggleLang(e){e.stopPropagation();const m=e.currentTarget.parentNode;const o=m.classList.toggle('open');e.currentTarget.setAttribute('aria-expanded',o?'true':'false')}
document.addEventListener('click',()=>document.querySelectorAll('.langmenu.open').forEach(m=>m.classList.remove('open')));
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.langmenu.open').forEach(m=>m.classList.remove('open'))});
</script>
<body style="display:grid;place-items:center;min-height:100vh">
<div class="card" role="main" style="width:380px;max-width:95vw">
 <div style="display:flex;justify-content:space-between">
  <div><div class="mono" style="font-size:12px;color:var(--gold);letter-spacing:.12em">WIKI·CONTEST</div>
  <h1 style="margin:4px 0">{{t.brand}}</h1></div>
  <div style="display:flex;gap:6px;align-items:flex-start"><button type="button" class="themebtn btn-s" onclick="cycleTheme()" data-t="{{t.theme_auto}}|{{t.theme_light}}|{{t.theme_dark}}">◐</button>  <div class="langmenu login-lm"><button type="button" class="burger" aria-label="{{l|upper}} · {{t.lang_menu}}" title="{{t.lang_menu}}" aria-expanded="false" onclick="toggleLang(event)"><i></i><span>{{l|upper}}</span></button><div class="langsw"><a href="?lang=uk" class="{{'on' if l=='uk' else ''}}" style="color:var(--ink2)">УКР</a><a
   href="?lang=en" class="{{'on' if l=='en' else ''}}" style="color:var(--ink2)">ENG</a><a
   href="?lang=pl" class="{{'on' if l=='pl' else ''}}" style="color:var(--ink2)">PL</a></div></div></div>
 </div>
 <div class="hint" style="margin-bottom:12px">{{t.tagline}}</div>
 <form method="post">
  <label class="lbl" for="f_login_1">{{t.login}}</label><input id="f_login_1" name="login" autofocus>
  <label class="lbl" for="f_password_2">{{t.password}}</label><input id="f_password_2" name="password" type="password">
  {% if err %}<div style="color:var(--err);margin-top:8px">{{t.bad}}</div>{% endif %}
  <button class="btn-p" style="width:100%;margin-top:14px">{{t.signin}}</button>
 </form>
 {% if oauth_unknown %}
 <div style="color:var(--err);margin-top:12px;font-size:13px">{{t.oauth_unknown}} «{{oauth_unknown}}»</div>
 {% endif %}
 {% if oauth_enabled %}
 <div style="display:flex;align-items:center;gap:10px;margin:16px 0">
  <div style="flex:1;height:1px;background:var(--line)"></div><span class="hint" style="">{{t.or_}}</span><div style="flex:1;height:1px;background:var(--line)"></div>
 </div>
 <a href="{{ url_for('login_oauth') }}" class="btn-s" style="display:block;text-align:center;text-decoration:none;box-sizing:border-box">{{t.oauth_btn}}</a>
 {% endif %}
</div></body></html>"""

MAIN_HTML = """<!doctype html><html lang="{{l}}"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{t.brand}}</title><style>""" + BASE_CSS + """</style>
<script>
(function(){try{const v=localStorage.getItem('wr_theme');if(v==='light'||v==='dark')document.documentElement.setAttribute('data-theme',v)}catch(e){}})();
function themeNow(){const v=document.documentElement.getAttribute('data-theme');return v==='light'||v==='dark'?v:'auto'}
function paintTheme(){const m=themeNow(),ic={auto:'◐',light:'☀',dark:'☾'};document.querySelectorAll('.themebtn').forEach(b=>{const L=(b.getAttribute('data-t')||'').split('|');const nm=L[['auto','light','dark'].indexOf(m)]||m;b.textContent=ic[m];b.title=nm;b.setAttribute('aria-label',nm)})}
function cycleTheme(){const o={auto:'light',light:'dark',dark:'auto'},n=o[themeNow()];if(n==='auto')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',n);try{if(n==='auto')localStorage.removeItem('wr_theme');else localStorage.setItem('wr_theme',n)}catch(e){}paintTheme()}
document.addEventListener('DOMContentLoaded',paintTheme);
function toggleLang(e){e.stopPropagation();const m=e.currentTarget.parentNode;const o=m.classList.toggle('open');e.currentTarget.setAttribute('aria-expanded',o?'true':'false')}
document.addEventListener('click',()=>document.querySelectorAll('.langmenu.open').forEach(m=>m.classList.remove('open')));
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.langmenu.open').forEach(m=>m.classList.remove('open'))});
</script>
<body>
<header>
 <div><span class="mono" style="font-size:12px;color:var(--cdx-subtle);letter-spacing:.12em">WIKI·CONTEST</span>
 <h1 style="font-weight:800;font-size:18px;margin:0;font-family:inherit">{{t.brand}}</h1></div>
 {% if contests %}
 <form method="get" style="margin:0"><input type="hidden" name="view" value="{{view}}">
  <select name="contest" aria-label="{{t.contest_sel}}" onchange="this.form.submit()" style="width:auto">
  {% for c in contests %}<option value="{{c.id}}" {{'selected' if c.id==cid else ''}}>{{c.name}}</option>{% endfor %}
  </select></form>
 {% endif %}
 <nav>
  <a href="?view=analysis&contest={{cid or ''}}" class="{{'on' if view=='analysis' else ''}}">{{t.nav_a}}</a>
  {% if me.role=='admin' %} · <a href="?view=admin&contest={{cid or ''}}" class="{{'on' if view=='admin' else ''}}">{{t.nav_s}}</a>{% endif %}
 </nav>
 <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
  <button type="button" class="themebtn btn-s" onclick="cycleTheme()" data-t="{{t.theme_auto}}|{{t.theme_light}}|{{t.theme_dark}}">◐</button>
  <span class="langmenu"><button type="button" class="burger" aria-label="{{l|upper}} · {{t.lang_menu}}" title="{{t.lang_menu}}" aria-expanded="false" onclick="toggleLang(event)"><i></i><span>{{l|upper}}</span></button><span class="langsw"><a href="?lang=uk&view={{view}}&contest={{cid or ''}}" class="{{'on' if l=='uk' else ''}}">УКР</a><a
    href="?lang=en&view={{view}}&contest={{cid or ''}}" class="{{'on' if l=='en' else ''}}">ENG</a><a
    href="?lang=pl&view={{view}}&contest={{cid or ''}}" class="{{'on' if l=='pl' else ''}}">PL</a></span></span>
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

// ── Доступність: клавіатура та спливаючі підказки «?» ──
function kbd(e,fn){if(e.key==='Enter'||e.key===' '){e.preventDefault();fn()}}
let HELP_N=0;
function hintHelp(text,style){
 const t=String(text||''),m=t.match(/^(.+?[.!?])\\s+(.+)$/s);
 const st=style?' style="'+style+'"':'';
 if(t.length<=120||!m)return '<div class="hint"'+st+'>'+esc(t)+'</div>';
 const id='jh'+(++HELP_N);
 return '<div class="hint"'+st+'>'+esc(m[1])+' <span class="help"><button type="button" class="help-btn" aria-label="'+esc(T.help_btn)+'" aria-expanded="false" aria-describedby="'+id+'">?</button><span role="tooltip" id="'+id+'" class="help-pop" hidden>'+esc(t)+'</span></span></div>';
}
(function(){
 function pop(h){return h.querySelector('.help-pop')}
 function place(h){
  const b=h.querySelector('.help-btn'),p=pop(h);if(!b||!p||p.hidden)return;
  const r=b.getBoundingClientRect(),vw=innerWidth,vh=innerHeight;
  p.style.left='0px';p.style.top='0px';
  const w=p.offsetWidth,ph=p.offsetHeight,g=8;
  let x,y;
  if(r.right+g+w<=vw-8){x=r.right+g;y=Math.min(Math.max(8,r.top-4),vh-ph-8)}     // праворуч від «?»
  else if(r.top-g-ph>=8){x=Math.min(Math.max(8,r.left),vw-w-8);y=r.top-g-ph}       // над кнопкою
  else{x=Math.min(Math.max(8,r.left),vw-w-8);y=Math.min(r.bottom+g,vh-ph-8)}
  p.style.left=x+'px';p.style.top=y+'px';
 }
 function open(h,pin){const p=pop(h),b=h.querySelector('.help-btn');if(!p)return;
  document.querySelectorAll('.help.open').forEach(o=>{if(o!==h)close(o)});
  p.hidden=false;h.classList.add('open');b.setAttribute('aria-expanded','true');if(pin)h.dataset.pin='1';place(h)}
 function close(h){const p=pop(h),b=h.querySelector('.help-btn');if(!p)return;
  p.hidden=true;h.classList.remove('open');delete h.dataset.pin;b.setAttribute('aria-expanded','false')}
 const hp=e=>e.target.closest&&e.target.closest('.help');
 document.addEventListener('mouseover',e=>{const h=hp(e);if(h&&!h.classList.contains('open'))open(h,false)});
 document.addEventListener('mouseout',e=>{const h=hp(e);if(!h||h.dataset.pin)return;
  if(e.relatedTarget&&h.contains(e.relatedTarget))return;
  if(h.contains(document.activeElement)&&h.matches(':focus-within')&&document.activeElement.classList.contains('help-btn')&&h.dataset.kb)return;
  close(h)});
 document.addEventListener('focusin',e=>{const h=hp(e);if(h&&e.target.classList.contains('help-btn')){h.dataset.kb='1';open(h,false)}});
 document.addEventListener('focusout',e=>{const h=hp(e);if(!h)return;delete h.dataset.kb;
  if(e.relatedTarget&&h.contains(e.relatedTarget))return;if(!h.dataset.pin)close(h)});
 document.addEventListener('click',e=>{
  const h=hp(e);
  if(h&&e.target.classList.contains('help-btn')){e.preventDefault();
   if(h.classList.contains('open')&&h.dataset.pin)close(h);else open(h,true);return}
  document.querySelectorAll('.help.open').forEach(o=>{if(!o.contains(e.target))close(o)})});
 document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;
  const o=document.querySelectorAll('.help.open');if(!o.length)return;
  o.forEach(close);e.stopImmediatePropagation();e.preventDefault()},true);
 addEventListener('scroll',()=>document.querySelectorAll('.help.open').forEach(place),true);
 addEventListener('resize',()=>document.querySelectorAll('.help.open').forEach(place));
})();
function pbar(el,done,total,label){
 const pct=total?Math.round(done/total*100):0;
 el.innerHTML=`<div class="pbar" role="progressbar" aria-label="${esc(label||T.progress)}" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><i style="width:${pct}%"></i></div>
 <div class="hint mono" style="">${done} / ${total} · ${pct}% ${label?'· '+esc(label):''}</div>`;
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
 do{const params={action:'query',list:'usercontribs',ucuser:user,uclimit:'500',ucprop:'title|timestamp|sizediff|flags|ids|comment'};
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
   rvprop:'user|timestamp|size|flags|ids|comment',rvlimit:'500',rvdir:'older'};
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
  const existing=document.getElementById('tplReplace').checked?[]:ta.value.split('\\n').map(s=>s.trim()).filter(Boolean);
  ta.value=[...new Set([...existing,...users])].sort((a,b)=>a.localeCompare(b)).join('\\n');
  msg.textContent='✓ '+pages.length+' / '+users.size+(failed?' ('+failed+' сторінок пропущено через помилку)':'');
  const cidEl=document.querySelector('form input[name=id]'),cidV=cidEl?cidEl.value:'';
  if(cidV){
   try{
    const list=ta.value.split('\\n').map(x=>x.trim()).filter(Boolean);
    const rs=await fetch('/api/contest/'+encodeURIComponent(cidV)+'/participants',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({participants:list})});
    if(!rs.ok)throw new Error(rs.status);
    msg.textContent+=' · '+T.parts_saved+' ('+list.length+')';
   }catch(e2){msg.textContent+=' · ⚠ '+T.parts_not_saved}
  }else msg.textContent+=' · '+T.parts_save_hint;
 }catch(err){msg.textContent='✗ '+err.message}
 btn.disabled=false;
}
</script>
{% else %}
<script>
const CONTEST={{cjson|safe}};
let RESULTS={{rjson|safe}}, ASSESS={{ajson|safe}};
const JURY_LOGIN={{me.login|tojson}};
const IS_JURY={{(me.role=='jury')|tojson}};
const AUTORUN={{(autorun)|tojson}};
let QUANT=true,QUAL=IS_JURY;
const projects=(CONTEST?CONTEST.projects:[]).map(p=>{try{const u=new URL(p);
 return{origin:u.origin,api:u.origin+'/w/api.php',label:u.hostname.replace('.org',''),host:u.hostname}}catch(e){return null}}).filter(Boolean);

function setMode(m){if(m==='qual'){QUAL=!QUAL;if(QUAL)QUANT=true}else{if(!QUAL)QUANT=!QUANT}
 document.getElementById('mq').className=QUANT?'on':'';document.getElementById('ml').className=QUAL?'on':'';
 document.getElementById('modehint').textContent=QUAL?T.qual_h:T.modes_h;render()}

// хештег, який учасник має вказати в описі правки (напр. #1Lib1Ref): рахуємо правки з ним
function srcTag(){const t=String(CONTEST.src_tag||'').trim().replace(/^#+/,'').toLowerCase();return t?'#'+t:''}
function trackRev(d,ts,revid,parentid,comment){
 if(!d||!ts||!revid)return;
 (d._e=d._e||[]).push([parentid||0,revid]);
 const tg=srcTag();
 if(tg&&String(comment||'').toLowerCase().indexOf(tg)>=0)(d._te=d._te||[]).push([parentid||0,revid]);
}
function countRefs(t){return((t.match(/<ref[\\s>\\/]/gi)||[]).length)+((t.match(/\\{\\{\\s*(sfn|sfnp|harvnb|harv)\\s*\\|/gi)||[]).length)}
function countImgs(t){
 return((t.match(/\\[\\[\\s*(Файл|File|Image|Зображення)\\s*:/gi)||[]).length)
  +((t.match(/^\\s*(Файл|File|Image|Зображення)\\s*:[^\\n|\\]]+/gim)||[]).length)
  +((t.match(/\\|\\s*(зображення|image|файл|фото|photo)\\s*=\\s*[^|}\\n]+\\.(jpe?g|png|gif|svg|webp|tiff?)/gi)||[]).length)
}
// Статус доброї/вибраної визначаємо за історією сторінок проєкту: стаття є у списку на кінець кампанії і не було на її початок
const STATUS_DEF={featured:'Вікіпедія:Вибрані статті',good:'Вікіпедія:Добрі статті',list:'Вікіпедія:Вибрані списки'};
function parseStatusPages(val,kind){
 const out=[];
 let items=String(val||'').split('|').map(x=>x.trim()).filter(Boolean);
 if(!items.length&&kind==='list'&&projects.some(p=>p.host==='uk.wikipedia.org'))items=[STATUS_DEF[kind]];
 items.forEach(v=>{
  let host=null,title=v;
  if(/^https?:\\/\\//i.test(v)){
   try{const u=new URL(v);host=u.hostname;
    title=u.searchParams.get('title')||decodeURIComponent(u.pathname.replace(/^\\/wiki\\//,''))}catch(e){return}
  }
  out.push({host:host||(projects.find(p=>p.host==='uk.wikipedia.org')||projects[0]||{}).host,title:title.replace(/_/g,' ')});
 });
 return out;
}
const UK_MONTHS={січня:1,лютого:2,березня:3,квітня:4,травня:5,червня:6,липня:7,серпня:8,вересня:9,жовтня:10,листопада:11,грудня:12};
function parseUkDate(t){
 const m=/(\\d{1,2})\\s+([а-яіїєґ]+)\\s+(\\d{4})/i.exec(t||'');if(!m)return null;
 const mo=UK_MONTHS[m[2].toLowerCase()];if(!mo)return null;
 return m[3]+'-'+String(mo).padStart(2,'0')+'-'+m[1].padStart(2,'0');
}
const VZV_FEATURED=['статус вибраної надано','підвищено до статусу вибраної'];
const VZV_GOOD=['статус доброї надано'];
// події шаблону Повідомлення ВЗВ/Подія (тип, дата, посилання) на сторінці обговорення
function parseVzvEvents(txt){
 const out=[];const re=/\\{\\{\\s*Повідомлення ВЗВ\\/Подія\\s*\\|([^|}]*)\\|([^|}]*)/g;let m;
 while((m=re.exec(txt||''))){out.push({ev:m[1].trim().toLowerCase(),date:parseUkDate(m[2])})}
 return out;
}
function statusFromEvents(evs,sDay,eDay){
 let kind=null,date=null;
 evs.forEach(e=>{
  if(!e.date||(sDay&&e.date<sDay)||(eDay&&e.date>eDay))return;
  const k=VZV_FEATURED.includes(e.ev)?'featured':(VZV_GOOD.includes(e.ev)?'good':null);
  if(!k)return;
  if(k==='featured'||!kind){if(!(kind==='featured'&&k==='good')){kind=k;date=e.date}}
 });
 return kind?{kind,date}:null;
}
async function revAt(api,title,ts){
 const p={action:'query',prop:'revisions',titles:title,rvlimit:'1',rvprop:'ids|timestamp',rvdir:'older'};
 if(ts)p.rvstart=ts;
 const d=await mw(api,p);const pg=Object.values(d.query?.pages||{})[0];
 return pg&&pg.revisions&&pg.revisions[0]?pg.revisions[0].revid:null;
}
async function linksOfRev(api,revid){
 const set=new Set();
 const d=await mw(api,{action:'parse',oldid:revid,prop:'links'});
 (d.parse?.links||[]).forEach(l=>{if(l.ns===0)set.add(l['*'])});
 return set;
}
async function pageParts(api,hub){
 // сам список може лежати на підсторінках, які підключено до головної сторінки
 const out=[hub];let cont=null,g=0;
 const base=hub.replace(/^[^:]+:/,'').toLowerCase();
 do{const p={action:'query',prop:'templates',titles:hub,tllimit:'max'};if(cont)p.tlcontinue=cont;
  const d=await mw(api,p);const pg=Object.values(d.query?.pages||{})[0];
  (pg?.templates||[]).forEach(t=>{if(t.title!==hub&&t.title.toLowerCase().indexOf(base)>=0)out.push(t.title)});
  cont=d.continue?.tlcontinue||null;g++;
 }while(cont&&g<5);
 return out.slice(0,60);
}
async function addedDate(api,page,title,sISO,eISO){
 // орієнтовна дата: найраніша правка сторінки в період кампанії, у коментарі якої згадано статтю
 try{
  const p={action:'query',prop:'revisions',titles:page,rvlimit:'500',rvprop:'timestamp|comment',rvdir:'newer'};
  if(sISO)p.rvstart=sISO;if(eISO)p.rvend=eISO;
  const d=await mw(api,p);const pg=Object.values(d.query?.pages||{})[0];
  const t=title.toLowerCase();
  const r=(pg?.revisions||[]).find(x=>(x.comment||'').toLowerCase().indexOf(t)>=0);
  return r?r.timestamp:'';
 }catch(e){return''}
}
async function enrichExtras(out,bar){
 const tag=srcTag();
 const wantC=!!(CONTEST.opt_images||CONTEST.opt_refs),wantS=!!CONTEST.opt_status;
 const refEdits=it=>tag?(it._te||[]):(it._e||[]); // з хештегом аналізуємо лише правки з ним
 const lists=[];
 Object.values(out).forEach(row=>Object.entries(row.perProject||{}).forEach(([host,pp])=>(pp.list||[]).forEach(it=>lists.push([host,it]))));
 const apiOf=h=>{const p=projects.find(x=>x.host===h);return p?p.api:null};
 const sISO=CONTEST.start?new Date(CONTEST.start+'T00:00:00Z').toISOString():'';
 const eISO=CONTEST.end?new Date(CONTEST.end+'T23:59:59Z').toISOString():'';
 try{
  if(wantC){
   // кожну правку учасника порівнюємо з її попередньою версією: так додані джерела й зображення прив'язані саме до цього користувача
   const need={};
   const add=(h,pr)=>pr.forEach(id=>{if(id)(need[h]=need[h]||new Set()).add(id)});
   lists.forEach(([h,it])=>{
    if(CONTEST.opt_images)(it._e||[]).forEach(pr=>add(h,pr));
    if(CONTEST.opt_refs)refEdits(it).forEach(pr=>add(h,pr));
   });
   const hosts=Object.keys(need);
   const total=hosts.reduce((s,h)=>s+Math.ceil(need[h].size/20),0)||1;let done=0;
   const cnt={};
   for(const h of hosts){
    const api=apiOf(h);if(!api)continue;
    const ids=[...need[h]];
    for(let i=0;i<ids.length;i+=20){
     pbar(bar,done,total,T.extras_running);done++;
     try{
      const d=await mw(api,{action:'query',prop:'revisions',revids:ids.slice(i,i+20).join('|'),rvprop:'ids|content',rvslots:'main'});
      Object.values(d.query?.pages||{}).forEach(pg=>(pg.revisions||[]).forEach(rv=>{
       const txt=(rv.slots&&rv.slots.main&&(rv.slots.main['*']||rv.slots.main.content))||rv['*']||'';
       cnt[h+':'+rv.revid]={r:countRefs(txt),i:countImgs(txt)};
      }));
     }catch(e){}
     await sleep(120);
    }
   }
   lists.forEach(([h,it])=>{
    const sum=(edits,key)=>{let v=0,ok=0;
     edits.forEach(([par,rev])=>{const a=cnt[h+':'+rev];const b=par?cnt[h+':'+par]:{r:0,i:0};
      if(!a||!b)return;ok++;v+=Math.max(0,a[key]-b[key])});
     return{v,ok}};
    if(CONTEST.opt_refs){
     const re=refEdits(it);
     if(!re.length&&tag)it.refs=0; // жодної правки з хештегом
     else{const x=sum(re,'r');if(x.ok)it.refs=x.v}
    }
    if(CONTEST.opt_images){const x=sum(it._e||[],'i');if(x.ok)it.images=x.v}
   });
  }
  if(wantS){
   const byHost={};
   lists.forEach(([h,it])=>{const m=(byHost[h]=byHost[h]||new Map());if(!m.has(it.title))m.set(it.title,[]);m.get(it.title).push(it)});
   // добрі та вибрані статті: шаблон Повідомлення ВЗВ на сторінці обговорення, дата події в межах кампанії
   const ukItems=byHost['uk.wikipedia.org'];
   if(ukItems){
    const tt=[...ukItems.keys()];const api=apiOf('uk.wikipedia.org');
    for(let i=0;i<tt.length;i+=20){
     pbar(bar,i,tt.length,T.extras_running);
     try{
      const d=await mw(api,{action:'query',prop:'revisions',titles:tt.slice(i,i+20).map(x=>'Talk:'+x).join('|'),rvprop:'content',rvslots:'main'});
      Object.values(d.query?.pages||{}).forEach(pg=>{
       const rv=pg.revisions&&pg.revisions[0];if(!rv)return;
       const txt=(rv.slots&&rv.slots.main&&(rv.slots.main['*']||rv.slots.main.content))||rv['*']||'';
       const items=ukItems.get(pg.title.replace(/^[^:]+:/,''));if(!items)return;
       const st=statusFromEvents(parseVzvEvents(txt),CONTEST.start||'',CONTEST.end||'');
       if(st)items.forEach(it=>{it.st=st.kind;it.st_ts=st.date});
      });
     }catch(e){}
     await sleep(120);
    }
   }
   const kinds=['featured','good','list'];
   const cfg={featured:CONTEST.cat_featured,good:CONTEST.cat_good,list:CONTEST.cat_list};
   const state={}; // host -> kind -> {S,E,pagesE}
   let step=0;
   for(const kind of kinds){
    for(const sp of parseStatusPages(cfg[kind],kind)){
     const api=apiOf(sp.host);if(!api||!byHost[sp.host])continue;
     const st=((state[sp.host]=state[sp.host]||{})[kind]=state[sp.host][kind]||{S:new Set(),E:new Map(),api});
     try{
      const parts=await pageParts(api,sp.title);
      for(const pg of parts){
       pbar(bar,step++,parts.length*3,T.extras_running);
       const r0=sISO?await revAt(api,pg,sISO):null;
       const r1=await revAt(api,pg,eISO||null);
       let S0=new Set();
       if(r0){S0=await linksOfRev(api,r0);S0.forEach(x=>st.S.add(x))}
       if(r1){const E1=(r1===r0)?S0:await linksOfRev(api,r1);E1.forEach(x=>{if(!st.E.has(x))st.E.set(x,pg)})}
       await sleep(120);
      }
     }catch(e){}
    }
   }
   for(const host of Object.keys(state)){
    const api=apiOf(host);
    for(const [title,items] of byHost[host]){
     if(items.some(it=>it.st))continue; // статус уже визначено за шаблоном
     let found=null;
     for(const kind of kinds){
      const st=state[host][kind];if(!st)continue;
      if(st.E.has(title)&&!st.S.has(title)){found={kind,page:st.E.get(title)};break}
     }
     if(!found)continue;
     const ts=await addedDate(api,found.page,title,sISO,eISO);
     items.forEach(it=>{it.st=found.kind;it.st_ts=ts});
    }
   }
  }
 }finally{
  lists.forEach(([h,it])=>{delete it._e;delete it._te});
 }
}
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
      trackRev(pp._detail[tplPages[i]],r.timestamp,r.revid,r.parentid,r.comment);
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
      trackRev(detail[c.title],c.timestamp,c.revid,c.parentid,c.comment);
     });
     const list=Object.entries(detail).map(([title,d])=>({title,bytes:d.bytes,created:d.created,_e:d._e,_te:d._te}))
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
   pp.list=Object.entries(pp._detail).map(([title,d])=>({title,bytes:d.bytes,created:d.created,_e:d._e,_te:d._te}))
    .sort((a,b2)=>b2.bytes-a.bytes);
   pp.articles=pp._titles.size;delete pp._detail;delete pp._titles;
  }
  row.articles=row._titles.size;delete row._titles;
 });
 pbar(bar,totalSteps,totalSteps,'');
 await enrichExtras(out,bar);
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
const SCOPE={{((contest.review_scope or 'all') if contest else 'all')|tojson}};
const MIN_BYTES={{((contest.min_bytes or 0) if contest else 0)|int}}; // мінімальний внесок учасника (налаштування конкурсу; 0 = вимкнено)
const MIN_EXP={{((contest.min_expand or 0) if contest else 0)|int}}; // мінімум доданих байтів, щоб доповнення потрапило на перевірку
const MIN_NEW={{((contest.min_created or 0) if contest else 0)|int}}; // мінімум байтів для нової статті
let SORT_KEY=null; // null (за замовчуванням) | 'qc' (бал нових) | 'qe' (бал доповнених)
let TOPN=10;
const BU=LANG==='uk'?' Б':' B';
const SF={refs:{{(1 if (contest and contest.scope_refs) else 0)}},images:{{(1 if (contest and contest.scope_images) else 0)}}};
let PATROL={{pjson|safe}};
let SHOW_STATUS=false,SHOW_PATROL=false;
const LEGACY={{ljson|safe}}; // оцінки/коментарі з попередньої версії (рівень учасника)
function inScope(it){
 if((it.bytes||0)<(it.created?MIN_NEW:MIN_EXP))return false;
 return(SCOPE==='all'||(SCOPE==='created')===!!it.created)&&(!SF.refs||(it.refs||0)>0)&&(!SF.images||(it.images||0)>0)}
function setSort(k){SORT_KEY=SORT_KEY===k?null:k;ARTOPEN=null;render();const b=document.querySelector('[data-sk="'+k+'"]');if(b)b.focus()}
function setTopN(n){TOPN=Number(n)||10;render()}
// середній бал журі окремо для нових і доповнених статей (у межах обсягу перевірки)
function kindQuality(r,user){
 const acc={created:[],expanded:[]};const seen=new Set();
 if(r)Object.values(r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{
  if(!inScope(it)||seen.has(it.title))return;seen.add(it.title);
  const vals=Object.values(slot(user,it.title).scores||{}).filter(v=>v!=null);
  if(vals.length)acc[it.created?'created':'expanded'].push(vals.reduce((x,y)=>x+y,0)/vals.length)}));
 const m=a=>a.length?a.reduce((x,y)=>x+y,0)/a.length:null;
 return{qc:m(acc.created),qcn:acc.created.length,qe:m(acc.expanded),qen:acc.expanded.length};
}
// рейтинг статей за середнім балом журі: окремо створені й окремо доповнені
function topArticles(rows){
 const out={created:[],expanded:[]};
 rows.forEach(row=>projects.forEach(p=>{
  const pp=row.r?row.r.perProject[p.host]:null;
  (pp&&pp.list?pp.list:[]).filter(inScope).forEach(it=>{
   const vals=Object.values(slot(row.user,it.title).scores||{}).filter(v=>v!=null);
   if(!vals.length)return;
   out[it.created?'created':'expanded'].push({user:row.user,title:it.title,origin:p.origin,bytes:it.bytes,
    avg:vals.reduce((x,y)=>x+y,0)/vals.length,n:vals.length});
  })}));
 const cmp=(a,b)=>(b.avg-a.avg)||(b.n-a.n)||(b.bytes-a.bytes);
 out.created.sort(cmp);out.expanded.sort(cmp);return out;
}
let TOP_ROWS=[];
// вивантаження повних рейтингів (усі оцінені статті) у CSV з BOM для Excel
function exportTop(){
 const tp=topArticles(TOP_ROWS),NL=String.fromCharCode(13,10);
 const q=v=>'"'+String(v).replace(/"/g,'""')+'"';
 const lines=[['#',T.top_c_type,T.top_c_article,T.th_u,T.th_b,T.top_c_score,T.top_c_n,T.top_c_link].map(q).join(',')];
 [['created',T.created],['expanded',T.expanded]].forEach(([k,lbl])=>tp[k].forEach((x,i)=>
  lines.push([i+1,lbl,x.title,x.user,x.bytes,x.avg.toFixed(2),x.n,articleLink(x.origin,x.title)].map(q).join(','))));
 const a=document.createElement('a');
 a.href=URL.createObjectURL(new Blob([String.fromCharCode(0xFEFF)+lines.join(NL)],{type:'text/csv;charset=utf-8'}));
 a.download='top-articles-'+CID+'.csv';document.body.appendChild(a);a.click();a.remove();
}
function topHtml(rows){
 TOP_ROWS=rows;
 const tp=topArticles(rows);
 const mx=Math.max(1,...tp.created.concat(tp.expanded).map(x=>x.avg));
 const col=(lbl,list,clr)=>{
  let h='<div style="flex:1;min-width:300px"><div class="lbl" style="margin:0 0 6px;color:'+clr+'">'+esc(lbl)+' <span class="hint">('+list.length+')</span></div>';
  if(!list.length)return h+'<div class="hint">'+esc(T.top_none)+'</div></div>';
  list.slice(0,TOPN).forEach((x,i)=>{
   h+='<div style="display:grid;grid-template-columns:22px 1fr 44px;gap:8px;align-items:center;padding:3px 0">'
    +'<span class="mono hint">'+(i+1)+'</span><div style="min-width:0">'
    +'<a class="wl" target="_blank" rel="noopener" href="'+articleLink(x.origin,x.title)+'" style="display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(x.title)+'</a>'
    +'<div class="hint" style="">'+esc(x.user)+' · +'+fmtN(x.bytes)+BU+' · '+x.n+' '+esc(T.scores_n)+'</div>'
    +'<div class="bar"><i style="width:'+(x.avg/mx*100)+'%;background:'+clr+'"></i></div></div>'
    +'<span class="mono" style="font-weight:700;text-align:right">'+x.avg.toFixed(2)+'</span></div>'});
  return h+'</div>'};
 let h='<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px"><h2 style="margin:0">'+esc(T.top_title)+'</h2>'
  +'<span style="flex:1 1 260px">'+hintHelp(T.top_h)+'</span><span style="margin-left:auto;display:flex;gap:10px;align-items:center" class="hint"><button class="btn-s" style="padding:4px 10px;font-size:12px" title="'+esc(T.tip_top_csv)+'" onclick="exportTop()">'+esc(T.top_csv)+'</button>'+esc(T.top_show)+' <select aria-label="'+esc(T.top_show)+'" onchange="setTopN(this.value)" style="width:auto;padding:2px 6px">'
  +[5,10,25,50].map(n=>'<option value="'+n+'"'+(n===TOPN?' selected':'')+'>'+n+'</option>').join('')+'</select></span></div>';
 h+='<div style="display:flex;flex-wrap:wrap;gap:24px">'+col(T.top_created,tp.created,'var(--cdx-ok-text)')+col(T.top_expanded,tp.expanded,'var(--acc)')+'</div>';
 return h;
}
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
async function runReassign(){
 const from=document.getElementById('reFrom').value,to=document.getElementById('reTo').value;
 const msg=document.getElementById('distmsg');
 if(!from||!to){msg.textContent='✗ '+T.reassign_pick;return}
 if(from===to){msg.textContent='✗ '+T.reassign_same;return}
 const call=async dry=>{const r=await fetch('/api/reassign/'+CID,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({from:from,to:to,dry:dry})});const d=await r.json();
  if(!r.ok||d.error)throw new Error(T[d.error]||d.error||r.status);return d};
 try{
  msg.textContent=T.loading;
  const p=await call(true);
  if(!p.movable){msg.textContent=T.reassign_none+(p.kept?' ('+p.kept+')':'');return}
  if(!confirm(T.reassign_confirm.replace('{n}',p.movable).replace('{k}',p.kept))){msg.textContent='';return}
  const d=await call(false);
  await reloadState();render();
  msg.textContent='✓ '+T.reassign_done+': '+(d.moved+d.merged)+' — '+juryName(from)+' → '+juryName(to);
 }catch(e){msg.textContent='✗ '+e.message}
}
function toggleBox(w){
 if(w==='status')SHOW_STATUS=!SHOW_STATUS;else SHOW_PATROL=!SHOW_PATROL;
 const bs=document.getElementById('ms'),bp=document.getElementById('mp');
 if(bs)bs.className=SHOW_STATUS?'on':'';if(bp)bp.className=SHOW_PATROL?'on':'';
 renderStatusBox();renderPatrolBox();
}
function artLink(origin,title){return origin+'/wiki/'+encodeURIComponent(String(title).replace(/ /g,'_'))}
function renderStatusBox(){
 const box=document.getElementById('statusbox');if(!box)return;
 box.style.display=SHOW_STATUS?'block':'none';if(!SHOW_STATUS)return;
 const m=new Map();
 CONTEST.participants.forEach(u=>{const r=RESULTS?RESULTS[u]:null;if(!r)return;
  projects.forEach(p=>{const pp=(r.perProject||{})[p.host];((pp&&pp.list)||[]).forEach(it=>{
   if(!it.st)return;const k=p.host+'::'+it.title;let e=m.get(k);
   if(!e){e={title:it.title,origin:p.origin,st:it.st,ts:it.st_ts,users:[]};m.set(k,e)}
   e.users.push(u)})})});
 const arr=[...m.values()].sort((a,b)=>(b.ts||'').localeCompare(a.ts||''));
 const nF=arr.filter(e=>e.st==='featured').length,nL=arr.filter(e=>e.st==='list').length,nG=arr.length-nF-nL;
 let h='<div class="lbl" style="margin:0" title="'+esc(T.tip_status_box)+'">'+T.status_title+'</div><div class="hint">'+T.status_h+'</div>';
 if(!(CONTEST.cat_featured||CONTEST.cat_good||CONTEST.cat_list||projects.some(p=>p.host==='uk.wikipedia.org')))h+='<div class="hint" style="margin-top:8px">'+T.status_nocat+'</div>';
 else if(!arr.length)h+='<div class="hint" style="margin-top:8px">'+T.status_none+'</div>';
 else{
  h+='<div class="hint" style="margin-top:6px">'+T.status_featured+': <b>'+nF+'</b> · '+T.status_good+': <b>'+nG+'</b> · '+T.status_list+': <b>'+nL+'</b></div>';
  h+='<table style="margin-top:8px"><thead><tr><th>'+T.th_article+'</th><th>'+T.th_status_kind+'</th><th>'+T.th_date+'</th><th>'+T.th_authors+'</th></tr></thead><tbody>';
  arr.forEach(e=>{h+='<tr><td><a class="wl" target="_blank" rel="noopener" href="'+artLink(e.origin,e.title)+'">'+esc(e.title)+' ↗</a></td><td>'+(e.st==='featured'?T.status_featured:(e.st==='list'?T.status_list:T.status_good))+'</td><td class="mono">'+esc((e.ts||'').slice(0,10))+'</td><td>'+e.users.map(esc).join(', ')+'</td></tr>'});
  h+='</tbody></table>';
 }
 box.innerHTML=h;
}
const normU=u=>String(u||'').replace(/_/g,' ').trim().toLowerCase();
async function runPatrol(){
 const btn=document.getElementById('patbtn'),bar=document.getElementById('patbar'),msg=document.getElementById('patmsg');
 const arts=new Map();
 CONTEST.participants.forEach(u=>{const r=RESULTS?RESULTS[u]:null;if(!r)return;
  Object.entries(r.perProject||{}).forEach(([h,pp])=>((pp&&pp.list)||[]).forEach(it=>arts.set(h+'::'+it.title,{host:h,title:it.title})))});
 const keys=[...arts.keys()];
 if(!keys.length){msg.textContent=T.no_results;return}
 const startISO=CONTEST.start?new Date(CONTEST.start+'T00:00:00Z').toISOString():null;
 const endD=CONTEST.patrol_end||CONTEST.end;
 const endISO=endD?new Date(endD+'T23:59:59Z').toISOString():null;
 btn.disabled=true;msg.textContent=T.patrol_running;
 const out={};let done=0,failed=0,idx=0;
 const worker=async()=>{
  while(idx<keys.length){
   const k=keys[idx++],a=arts.get(k),pr=projects.find(x=>x.host===a.host);
   try{
    const params={action:'query',list:'logevents',letype:'patrol',letitle:a.title,lelimit:'500'};
    if(endISO)params.lestart=endISO;if(startISO)params.leend=startISO;
    const d=await mw(pr.api,params);
    out[k]=(d.query?.logevents||[]).filter(e=>e.user&&!e.actionhidden).map(e=>({u:e.user,t:e.timestamp,a:(e.params&&e.params.auto)?1:0}));
   }catch(e){failed++}
   done++;if(done%10===0||done===keys.length)pbar(bar,done,keys.length,a.title);
   await sleep(80);
  }
 };
 try{
  await Promise.all([worker(),worker(),worker(),worker()]);
  PATROL={articles:out,updated:Math.floor(Date.now()/1000),deadline:endD||'',failed,total:keys.length};
  await fetch('/api/patrol/'+CID,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(PATROL)});
  msg.textContent='';
 }catch(e){msg.textContent='✗ '+e.message}
 btn.disabled=false;renderPatrolBox();
}
function renderPatrolBox(){
 const box=document.getElementById('patrolbox');if(!box)return;
 box.style.display=SHOW_PATROL?'block':'none';if(!SHOW_PATROL)return;
 const dl=CONTEST.patrol_end||CONTEST.end||'';
 let h='<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end"><div><div class="lbl" style="margin:0" title="'+esc(T.tip_patrol_box)+'">'+T.patrol_title+'</div><div class="hint">'+T.patrol_h+(dl?' · '+T.patrol_deadline+': <b>'+esc(dl)+'</b>':'')+'</div></div>'
  +'<button id="patbtn" class="btn-p" style="margin-left:auto" title="'+esc(T.tip_patrol_run)+'" onclick="runPatrol()">'+T.patrol_run+'</button></div>'
  +'<div id="patbar" style="margin-top:6px"></div><div id="patmsg" class="hint mono"></div>';
 if(!PATROL||!PATROL.articles){box.innerHTML=h+'<div class="hint" style="margin-top:8px">'+T.patrol_nodata+'</div>';return}
 const names=new Map();CONTEST.participants.forEach(u=>names.set(normU(u),u));
 const authors=new Map(); // стаття -> автори (учасники кампанії, які її створили чи доповнили)
 CONTEST.participants.forEach(u=>{const r=RESULTS?RESULTS[u]:null;if(!r)return;
  Object.entries(r.perProject||{}).forEach(([hh,pp])=>((pp&&pp.list)||[]).forEach(it=>{
   const k=hh+'::'+it.title;if(!authors.has(k))authors.set(k,new Set());authors.get(k).add(normU(u))}))});
 const partSet=new Set(names.keys());
 let pat=0,unk=0;const unp=[],byP=new Map(),perArt=[],perAuthor=new Map();
 const ensureA=n=>{let a=perAuthor.get(n);if(!a){a={total:0,pat:0,auto:0,out:0};perAuthor.set(n,a)}return a};
 authors.forEach((us,k)=>{
  const ev=PATROL.articles[k];
  if(!ev){unk++;us.forEach(n=>ensureA(n).total++);return}
  us.forEach(n=>ensureA(n).total++);
  if(!ev.length){unp.push(k);return}
  pat++;const seen=new Map();
  ev.forEach(e=>{const n=normU(e.u);const prev=seen.get(n);if(!prev)seen.set(n,{u:e.u,t:e.t,a:e.a?1:0});else if(e.a)prev.a=1});
  perArt.push([k,[...seen.values()]]);
  us.forEach(n=>{const a=ensureA(n);a.pat++;
   const own=seen.get(n);
   if(own&&own.a)a.auto++;
   if([...seen.keys()].some(x=>!us.has(x)&&!partSet.has(x)))a.out++});
  seen.forEach((v,n)=>{let p=byP.get(n);if(!p){p={u:v.u,all:0,own:0,auto:0};byP.set(n,p)}p.all++;if(us.has(n))p.own++;if(v.a)p.auto++});
 });
 const total=authors.size,left=total-pat-unk,pct=total?Math.round(pat/total*100):0;
 const oOf=k=>{const [hh,...rest]=k.split('::');const p=projects.find(x=>x.host===hh);return{origin:p?p.origin:'',title:rest.join('::')}};
 h+='<div style="margin-top:10px"><div class="hint mono">'+T.patrol_patrolled+': <b>'+pat+'</b> · '+T.patrol_left+': <b>'+left+'</b> · '+T.patrol_total+': '+total+' · '+pct+'%'+(unk?' · '+T.patrol_unknown+': '+unk:'')+'</div><div class="pbar"><i style="width:'+pct+'%"></i></div>'
  +'<div class="hint">'+T.patrol_updated+': '+new Date((PATROL.updated||0)*1000).toLocaleString(LANG==='uk'?'uk-UA':'en-GB')+'</div></div>';
 // 1) учасники кампанії: стан патрулювання їхніх статей
 const A=[...perAuthor.entries()].map(([n,a])=>({u:names.get(n)||n,...a})).sort((x,y)=>(y.total-y.pat)-(x.total-x.pat));
 h+='<div class="lbl" style="margin-top:14px" title="'+esc(T.tip_patrol_authors)+'">'+T.patrol_authors+' ('+A.length+')</div>';
 h+='<div style="max-height:340px;overflow:auto"><table><thead><tr><th>'+T.th_u+'</th><th>'+T.patrol_arts+'</th><th>'+T.patrol_patrolled+'</th><th>'+T.patrol_left+'</th><th title="'+esc(T.tip_patrol_auto)+'">'+T.patrol_auto_col+'</th><th title="'+esc(T.tip_patrol_by_out)+'">'+T.patrol_by_out+'</th></tr></thead><tbody>';
 A.forEach(a=>{h+='<tr><td>'+esc(a.u)+'</td><td class="mono">'+a.total+'</td><td class="mono">'+a.pat+'</td><td class="mono">'+(a.total-a.pat)+'</td><td class="mono">'+a.auto+'</td><td class="mono">'+a.out+'</td></tr>'});
 h+='</tbody></table></div>';
 // 2) хто патрулював: учасники кампанії проти сторонніх
 const P=[],O=[];byP.forEach((p,n)=>(partSet.has(n)?P:O).push(p));
 P.sort((a,b)=>b.all-a.all);O.sort((a,b)=>b.all-a.all);
 h+='<div class="lbl" style="margin-top:14px" title="'+esc(T.tip_patrol_part)+'">'+T.patrol_participants+' ('+P.length+')</div>';
 if(!P.length)h+='<div class="hint">'+T.patrol_none+'</div>';
 else{h+='<table><thead><tr><th>'+T.th_u+'</th><th>'+T.patrol_total+'</th><th>'+T.patrol_own+'</th><th>'+T.patrol_others+'</th><th title="'+esc(T.tip_patrol_auto)+'">'+T.patrol_auto_col+'</th></tr></thead><tbody>';
  P.forEach(p=>{h+='<tr><td>'+esc(p.u)+'</td><td class="mono">'+p.all+'</td><td class="mono">'+p.own+'</td><td class="mono">'+(p.all-p.own)+'</td><td class="mono">'+p.auto+'</td></tr>'});h+='</tbody></table>'}
 h+='<div class="lbl" style="margin-top:14px" title="'+esc(T.tip_patrol_out)+'">'+T.patrol_outsiders+' ('+O.length+')</div>';
 if(!O.length)h+='<div class="hint">'+T.patrol_none+'</div>';
 else{h+='<table><thead><tr><th>'+T.th_u+'</th><th>'+T.patrol_total+'</th></tr></thead><tbody>';
  O.forEach(p=>{h+='<tr><td>'+esc(p.u)+'</td><td class="mono">'+p.all+'</td></tr>'});h+='</tbody></table>'}
 h+='<details style="margin-top:12px"><summary class="lbl" style="cursor:pointer">'+T.patrol_unpatrolled+' ('+unp.length+')</summary>';
 h+=unp.length?'<div style="columns:2;font-size:13px;padding:6px 0">'+unp.map(k=>{const o=oOf(k);return'<div><a class="wl" target="_blank" rel="noopener" href="'+artLink(o.origin,o.title)+'">'+esc(o.title)+' ↗</a></div>'}).join('')+'</div>':'<div class="hint">—</div>';
 h+='</details>';
 h+='<details style="margin-top:8px"><summary class="lbl" style="cursor:pointer">'+T.patrol_by_article+' ('+perArt.length+')</summary><table><thead><tr><th>'+T.th_article+'</th><th>'+T.patrol_by+'</th></tr></thead><tbody>'
  +perArt.map(([k,vs])=>{const o=oOf(k);return'<tr><td><a class="wl" target="_blank" rel="noopener" href="'+artLink(o.origin,o.title)+'">'+esc(o.title)+' ↗</a></td><td>'+vs.map(v=>esc(v.u)+(v.a?' ('+T.patrol_auto_short+')':'')+' <span class="hint mono">'+esc((v.t||'').slice(0,10))+'</span>').join(', ')+'</td></tr>'}).join('')
  +'</tbody></table></details>';
 box.innerHTML=h;
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
  h='<table><thead><tr><th>'+T.th_jury+'</th><th>'+T.th_assigned+'</th><th>'+T.th_reviewed+'</th><th style="width:35%"><span class="sr-only">'+esc(T.progress)+'</span></th></tr></thead><tbody>';
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
let MODAL_OPENER=null;
function closeModal(){MODAL=null;renderModal()}
// Вікіпедія забороняє показ сторінки історії в iframe (X-Frame-Options),
// тому вкладку «Стаття» показуємо в iframe, а історію будуємо з REST API MediaWiki.
const HIST_CACHE={},LEFT_PENDING=new Set();
async function ensureLeftData(){
 if(!MODAL)return;
 const m=MODAL,key=m.host+'::'+m.title;
 const still=()=>MODAL&&MODAL.host===m.host&&MODAL.title===m.title;
 if(MTAB==='history'&&!HIST_CACHE[key]&&!LEFT_PENDING.has('h'+key)){
  LEFT_PENDING.add('h'+key);
  try{
   let rows=[];
   try{
    // MediaWiki REST API: /w/rest.php/v1/page/{title}/history (до 3 сторінок по 20 правок)
    let url=m.origin+'/w/rest.php/v1/page/'+encodeURIComponent(m.title.replace(/ /g,'_'))+'/history';
    for(let g=0;g<3&&url;g++){
     const r=await fetch(url);
     if(!r.ok)throw new Error('HTTP '+r.status);
     const d=await r.json();
     (d.revisions||[]).forEach(x=>rows.push({revid:x.id,timestamp:x.timestamp,user:x.user?x.user.name:'',comment:x.comment||'',
      diff:(x.delta!=null?x.delta:x.size),size:x.size}));
     url=d.older||null;
    }
   }catch(e1){
    const d=await mw(m.api,{action:'query',prop:'revisions',titles:m.title,rvprop:'ids|user|timestamp|comment|size',rvlimit:'50',redirects:'1'});
    const page=Object.values(d.query?.pages||{})[0];
    rows=(page&&page.revisions)||[];
    for(let i=0;i<rows.length;i++)rows[i].diff=rows[i+1]?rows[i].size-rows[i+1].size:rows[i].size;
   }
   HIST_CACHE[key]={rows};
  }catch(e){HIST_CACHE[key]={err:String(e.message||e)}}
  LEFT_PENDING.delete('h'+key);if(still())renderModal();
 }
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&MODAL)closeModal()});
document.addEventListener('keydown',e=>{if(e.key!=='Tab'||!MODAL)return;
 const bx=document.getElementById('mdBox');if(!bx)return;
 const f=[...bx.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])')].filter(x=>x.offsetParent!==null);
 if(!f.length){e.preventDefault();return}
 const a=document.activeElement,first=f[0],last=f[f.length-1];
 if(e.shiftKey&&(a===first||a===bx)){e.preventDefault();last.focus()}
 else if(!e.shiftKey&&a===last){e.preventDefault();first.focus()}});
function leftHtml(ex){
 const m=MODAL;
 let h='<div class="md-tabs">';
 [['article',T.tab_article,T.tip_tab_article],['history',T.tab_history,T.tip_tab_history],['preview',T.tab_preview,T.tip_tab_preview]].forEach(x=>{
  h+='<button data-tab="'+x[0]+'" title="'+esc(x[2])+'" class="'+(MTAB===x[0]?'on':'')+'">'+esc(x[1])+'</button>'});
 h+='<a class="hint" style="margin-left:auto" target="_blank" rel="noopener" title="'+esc(T.iframe_hint)+'" href="'+esc(articleLink(m.origin,m.title))+'">'+T.open_new+'</a></div>';
 if(MTAB==='preview'){
  h+='<div class="md-pane">';
  if(!ex||ex.extract==null)h+='<div class="hint">'+T.loading+'</div>';
  else if(ex.err)h+='<div class="hint">'+esc(ex.err)+'</div>';
  else{
   if(ex.thumb)h+='<img src="'+esc(ex.thumb)+'" alt="" style="max-width:100%;border-radius:2px;margin-bottom:10px;display:block">';
   h+='<h2 style="margin:0 0 8px">'+esc(m.title)+'</h2>';
   h+='<div style="font-size:14px;line-height:1.55;white-space:pre-wrap">'+esc(ex.extract||'')+'</div>';
  }
  h+='</div>';
 }else if(MTAB==='history'){
  const hs=HIST_CACHE[m.host+'::'+m.title];
  h+='<div class="md-pane">';
  if(!hs)h+='<div class="hint">'+T.loading+'</div>';
  else if(hs.err)h+='<div class="hint">'+esc(hs.err)+'</div>';
  else{
   const enc=encodeURIComponent(m.title.replace(/ /g,'_'));
   h+='<table><thead><tr><th>'+T.hist_date+'</th><th>'+T.hist_user+'</th><th>'+T.hist_size+'</th><th>'+T.hist_comment+'</th></tr></thead><tbody>';
   hs.rows.forEach(r=>{
    const d=(r.timestamp||'').replace('T',' ').replace('Z','').slice(0,16);
    const col=r.diff>0?'var(--ok)':(r.diff<0?'var(--err)':'var(--mut)');
    h+='<tr><td class="mono" style="font-size:12px;white-space:nowrap"><a class="wl" target="_blank" rel="noopener" href="'+esc(m.origin+'/w/index.php?title='+enc+'&diff=prev&oldid='+r.revid)+'">'+esc(d)+'</a></td>'
     +'<td>'+esc(r.user||'')+'</td>'
     +'<td class="mono" style="font-size:12px;color:'+col+'">'+(r.diff>0?'+':'')+fmtN(r.diff)+'</td>'
     +'<td class="hint">'+esc(r.comment||'')+'</td></tr>';
   });
   h+='</tbody></table>';
  }
  h+='</div>';
 }else{
  h+='<iframe class="md-frame" src="'+esc(articleLink(m.origin,m.title)+'?useskin=minerva')+'" title="'+esc(T.dlg_frame+': '+m.title)+'" sandbox="allow-same-origin allow-scripts allow-popups allow-popups-to-escape-sandbox"></iframe>';
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
 h+='<button class="btn-s" title="'+esc(T.tip_prev)+'" onclick="stepModal(-1)"'+(idx>0?'':' disabled')+'>‹ '+T.prev+'</button>';
 h+='<button class="btn-s" title="'+esc(T.tip_next)+'" onclick="stepModal(1)"'+(idx>=0&&idx<CUR_LIST.length-1?'':' disabled')+'>'+T.next+' ›</button>';
 h+='<span class="hint mono">'+(idx>=0?(idx+1)+' / '+CUR_LIST.length:'')+'</span>';
 h+='<button class="btn-s" style="margin-left:auto;padding:4px 10px" title="'+esc(T.tip_close)+'" onclick="closeModal()">✕</button></div>';
 h+='<h2 style="margin:4px 0 6px">'+esc(m.title)+'</h2>';
 h+='<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
 h+='<span class="chip" style="background:'+(m.created?'var(--cdx-ok-subtle)':'var(--acc-soft)')+';color:'+(m.created?'var(--cdx-ok-text)':'var(--acc)')+'">'+(m.created?T.created:T.expanded)+'</span>';
 h+='<span class="chip '+(rev?'st-ok':'st-todo')+'"'+(rev&&!legacyDone(m.user)?' style="cursor:pointer" title="'+esc(T.tip_unrev)+'" role="button" tabindex="0" onkeydown="kbd(event,toggleReviewed)" onclick="toggleReviewed()"':' title="'+esc(T.tip_status)+'"')+'>'+(rev?'✓ '+T.reviewed:'● '+T.unreviewed)+'</span>';
 h+='<span class="hint mono">'+esc(m.label)+' · +'+fmtN(m.bytes)+(LANG==='uk'?' Б':' B')+'</span></div>';
 h+='<div class="hint">'+esc(m.user)+'</div>';
 if((ad.assigned||[]).length)h+='<div class="hint" style="margin-top:4px">'+T.assigned_to+': '+ad.assigned.map(l=>esc(juryName(l))).join(', ')+'</div>';
 h+='<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px">';
 h+='<label class="hint" style="" title="'+esc(T.tip_my_score)+'">'+T.th_my+':</label>';
 h+='<input type="number" step="0.1" aria-label="'+esc(T.tip_my_score)+'" title="'+esc(T.tip_my_score)+'" style="width:70px" value="'+(my??'')+'" onchange="saveScoreM(this.value)">';
 h+='<span class="hint mono">'+T.th_avg+': '+(avg!=null?avg.toFixed(2):'—')+(vals.length?' ('+vals.length+' '+T.scores_n+')':'')+'</span></div>';
 h+='<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">';
 h+='<button class="btn-p" title="'+esc(rev?T.tip_next_btn:T.tip_rev_next)+'" onclick="reviewAndNext()">'+(rev?T.next+' ›':T.rev_next)+'</button></div>';
 h+='<div style="margin-top:14px">';
 (ad.comments||[]).forEach(c=>{h+='<div style="padding:5px 0;border-bottom:1px dashed var(--line)"><b>'+esc(c.jury)+'</b> <span class="hint mono">'+esc(c.date)+'</span><div>'+esc(c.text)+'</div></div>'});
 if(!(ad.comments||[]).length)h+='<div class="hint" style="">'+T.no_comments+'</div>';
 h+='<div style="display:flex;gap:8px;margin-top:8px"><input class="md-cm" id="cm_'+cid+'" placeholder="'+esc(T.comment_ph)+'" onkeydown="if(event.keyCode===13)saveCommentM()"><button class="btn-p" title="'+esc(T.tip_comment)+'" onclick="saveCommentM()">'+T.save+'</button></div>';
 h+='</div>';
 return h;
}
function renderModal(){
 let el=document.getElementById('artModal');
 if(!MODAL){if(el){el.remove();document.querySelectorAll('header,main').forEach(n=>{n.inert=false});
   const o=MODAL_OPENER;MODAL_OPENER=null;
   if(o&&document.contains(o))o.focus();else{const m=document.querySelector('main');if(m){m.setAttribute('tabindex','-1');m.focus()}}}return}
 if(!el){
  el=document.createElement('div');el.id='artModal';
  el.innerHTML='<div class="md-back" onclick="if(event.target===this)closeModal()"><div class="md-box" role="dialog" aria-modal="true" tabindex="-1" id="mdBox"><div class="md-left" id="mdLeft"></div><div class="md-right" id="mdRight"></div></div></div>';
  document.body.appendChild(el);
  MODAL_OPENER=document.activeElement;
  document.querySelectorAll('header,main').forEach(n=>{n.inert=true});
  const bx=document.getElementById('mdBox');if(bx)bx.focus();
 }
 const bx2=document.getElementById('mdBox');if(bx2)bx2.setAttribute('aria-label',MODAL.title);
 const key=MODAL.host+'::'+MODAL.title;
 const ex=EXTRACT_CACHE[key];
 // ліва частина перебудовується лише при зміні статті/вкладки — щоб iframe не перезавантажувався
 const left=document.getElementById('mdLeft');
 const lk=key+'|'+MTAB+'|'+(MTAB==='preview'?(ex&&ex.extract!=null?'1':'0'):(MTAB==='article'?'':(HIST_CACHE[key]?'1':'0')));
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
 ensureLeftData();
}
function articleQuality(pa,user,allowed){
 if(!pa)return{quality:0,n:0};
 const avgs=[];let n=0;
 Object.entries(pa).forEach(([ttl,a])=>{
  if(allowed&&!allowed.has(ttl))return;
  const vals=Object.values(a.scores||{}).filter(v=>v!=null);
  if(vals.length){avgs.push(vals.reduce((x,y)=>x+y,0)/vals.length);n+=vals.length}
 });
 if(!avgs.length&&user){const lv=legacyVals(user);if(lv.length)return{quality:lv.reduce((x,y)=>x+y,0)/lv.length,n:lv.length}}
 return{quality:avgs.length?avgs.reduce((x,y)=>x+y,0)/avgs.length:0,n};
}
function evalFormula(f,v){
 if(!f||!String(f).trim())return null;
 const cleaned=f.replace(/\\b(bytes|edits|articles|quality|created|expanded|refs|images|good|featured|flist)\\b/g,'1');
 if(!/^[\\d\\s+\\-*/().,]+$/.test(cleaned))return null;
 try{const fn=new Function('bytes','edits','articles','quality','created','expanded','refs','images','good','featured','flist','return ('+f+')');
  const r=fn(v.bytes,v.edits,v.articles,v.quality,v.created||0,v.expanded||0,v.refs||0,v.images||0,v.good||0,v.featured||0,v.flist||0);
  return typeof r==='number'&&!isNaN(r)?r:null}catch(e){return null}
}
// розбивка внеску на нові та доповнені статті (з уже збережених списків)
function splitStats(r){
 const s={created:0,expanded:0,bc:0,be:0,refs:0,images:0,good:0,featured:0,flist:0};
 if(!r)return s;
 Object.values(r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{
  s.refs+=it.refs||0;s.images+=it.images||0;
  if(it.st==='good')s.good++;else if(it.st==='featured')s.featured++;else if(it.st==='list')s.flist++;
  if(it.created){if((it.bytes||0)>=MIN_NEW){s.created++;s.bc+=it.bytes}}
  else if((it.bytes||0)>=MIN_EXP){s.expanded++;s.be+=it.bytes}}));
 return s;
}
function partTitles(r){
 const s=new Set();
 if(r)Object.values(r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{if(inScope(it))s.add(it.title)}));
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
 const HAS_F=!!(CONTEST.formula&&CONTEST.formula.trim());
 const allRows=CONTEST.participants.map(user=>{
  const r=RESULTS?RESULTS[user]:null;const pa=ASSESS[user]||{};
  const{quality,n}=articleQuality(pa,user,((SCOPE!=='all'||SF.refs||SF.images||MIN_EXP>0||MIN_NEW>0)&&r)?new Set(partTitles(r)):null);
  const sp=splitStats(r);const kq=kindQuality(r,user);
  const score=r?evalFormula(CONTEST.formula,{bytes:r.bytes,edits:r.edits,articles:r.articles,quality,created:sp.created,expanded:sp.expanded,refs:sp.refs,images:sp.images,good:sp.good,featured:sp.featured,flist:sp.flist}):null;
  const titles=partTitles(r);
  const done=titles.filter(t=>isReviewed(user,t)).length;
  const mineT=titles.filter(t=>isMine(user,t));
  const mineDone=mineT.filter(t=>isReviewed(user,t)).length;
  return{user,r,pa,quality,n,score,sp,kq,total:IS_JURY?mineT.length:titles.length,done:IS_JURY?mineDone:done,mine:mineT.length,mineDone};
 }).sort((x,y)=>{
  if(QUAL&&SORT_KEY){const a=x.kq[SORT_KEY]??-1e18,b=y.kq[SORT_KEY]??-1e18;if(a!==b)return b-a}
  return(QUAL&&!IS_JURY&&HAS_F)?((y.score??-1e18)-(x.score??-1e18)):((y.r?y.r.bytes:-1)-(x.r?x.r.bytes:-1))});
 const hiddenN=allRows.filter(x=>x.r&&x.r.bytes<MIN_BYTES).length;
 const rows=allRows.filter(x=>!(x.r&&x.r.bytes<MIN_BYTES)&&!(IS_JURY&&QUAL&&x.r&&!x.mine));
 ROWS_CUR=rows;
 const maxB=Math.max(1,...rows.map(r=>r.r?r.r.bytes:0));
 const th=(lbl,tip)=>'<th title="'+esc(tip||'')+'">'+esc(lbl)+'</th>';
 const thS=(lbl,tip,k)=>'<th title="'+esc(tip||'')+'" aria-sort="'+(SORT_KEY===k?'descending':'none')+'" style="white-space:nowrap"><button type="button" class="thbtn" data-sk="'+k+'" aria-label="'+esc(lbl+' — '+T.sort_hint)+'" onclick="setSort(\\''+k+'\\')">'+esc(lbl)+(SORT_KEY===k?' ▼':' ⇅')+'</button></th>';
 let h='<table><thead><tr><th>#</th>'+th(T.th_u,T.tip_th_u);
 if(IS_JURY){
  h+=th(T.th_b,T.tip_th_b)+th(T.th_created_n,T.tip_th_created_n)+th(T.th_expanded_n,T.tip_th_expanded_n)+th(T.th_avg,T.tip_th_avg)+thS(T.th_avg_c,T.tip_th_avg_c,'qc')+thS(T.th_avg_e,T.tip_th_avg_e,'qe')+th(T.th_progress,T.tip_th_progress);
 }else{
  if(QUANT){h+=th(T.th_b,T.tip_th_b);projects.forEach(p=>h+='<th>'+esc(p.label)+'</th>');h+=th(T.th_e,T.tip_th_e)+th(T.th_split,T.tip_th_split)
   +(CONTEST.opt_refs?th(T.th_refs,T.tip_th_refs):'')+(CONTEST.opt_images?th(T.th_images,T.tip_th_images):'')+(CONTEST.opt_status?th(T.th_status,T.tip_th_status):'')}
  if(QUAL){h+=th(T.th_avg,T.tip_th_avg)+thS(T.th_avg_c,T.tip_th_avg_c,'qc')+thS(T.th_avg_e,T.tip_th_avg_e,'qe');if(HAS_F)h+=th(T.th_score,T.tip_th_score);h+=th(T.th_progress,T.tip_th_progress)}
 }
 h+='<th><span class="sr-only">'+esc(T.a11y_actions)+'</span></th></tr></thead><tbody>';
 if(!rows.length)h+='<tr><td colspan="99" class="hint" style="padding:20px">'+T.no_parts+'</td></tr>';
 rows.forEach((row,i)=>{
  h+='<tr><td class="mono" style="color:var(--mut)">'+(i+1)+'</td><td>';
  h+='<a class="wl" title="'+esc(T.tip_user_link)+'" target="_blank" rel="noopener" href="'+contribLink(projects[0]?projects[0].origin:'',row.user)+'">'+esc(row.user)+' ↗</a>';
  h+='<div style="display:flex;gap:8px">'+projects.map(p=>'<a class="hint" target="_blank" rel="noopener" href="'+contribLink(p.origin,row.user)+'">'+esc(p.label)+' ↗</a>').join('')+'</div></td>';
  if(IS_JURY){
   h+='<td><div class="mono" style="font-weight:700">'+(row.r?'+'+fmtN(row.r.bytes)+(LANG==='uk'?' Б':' B'):'—')+'</div></td>';
   h+='<td class="mono" style="font-size:12px;color:var(--cdx-ok-text)">'+(row.r?fmtN(row.sp.created):'—')+'</td>';
   h+='<td class="mono" style="font-size:12px">'+(row.r?fmtN(row.sp.expanded):'—')+'</td>';
   h+='<td class="mono">'+(row.n?row.quality.toFixed(2):'—')+'<div class="hint">'+row.n+' '+T.scores_n+'</div></td>';
   h+='<td class="mono">'+(row.kq.qc!=null?row.kq.qc.toFixed(2):'—')+'<div class="hint">'+row.kq.qcn+' '+T.th_created_n.toLowerCase()+'</div></td>';
   h+='<td class="mono">'+(row.kq.qe!=null?row.kq.qe.toFixed(2):'—')+'<div class="hint">'+row.kq.qen+' '+T.th_expanded_n.toLowerCase()+'</div></td>';
   h+='<td class="mono" style="font-size:12px">'+(row.total?row.done+' / '+row.total:'—')+'</td>';
  }
  if(QUANT&&!IS_JURY){
   h+='<td><div class="mono" style="font-weight:700">'+(row.r?'+'+fmtN(row.r.bytes)+(LANG==='uk'?' Б':' B'):'—')+'</div>';
   h+='<div class="bar"><i style="width:'+((row.r?row.r.bytes:0)/maxB*100)+'%"></i></div></td>';
   projects.forEach(p=>{const pp=row.r?row.r.perProject[p.host]:null;
    h+='<td class="mono" style="font-size:12px">'+(pp?(pp.err?'<span style="color:var(--err)" title="'+esc(pp.err)+'">'+T.err+'</span>':'+'+fmtN(pp.bytes)):'—')+'</td>'});
   h+='<td class="mono" style="font-size:12px">'+(row.r?fmtN(row.r.edits)+' / '+fmtN(row.r.articles):'—')+'</td>';
   h+='<td class="mono" style="font-size:12px">'+(row.r?'<span style="color:var(--cdx-ok-text)">'+fmtN(row.sp.created)+'</span> / '+fmtN(row.sp.expanded)
    +'<div class="hint">+'+fmtN(row.sp.bc)+' / +'+fmtN(row.sp.be)+'</div>':'—')+'</td>';
   if(CONTEST.opt_refs)h+='<td class="mono" style="font-size:12px">'+(row.r?'+'+fmtN(row.sp.refs):'—')+'</td>';
   if(CONTEST.opt_images)h+='<td class="mono" style="font-size:12px">'+(row.r?'+'+fmtN(row.sp.images):'—')+'</td>';
   if(CONTEST.opt_status)h+='<td class="mono" style="font-size:12px">'+(row.r?fmtN(row.sp.featured)+' / '+fmtN(row.sp.good)+' / '+fmtN(row.sp.flist):'—')+'</td>';
  }
  if(QUAL&&!IS_JURY){
   h+='<td class="mono">'+(row.n?row.quality.toFixed(2):'—')+'<div class="hint">'+row.n+' '+T.scores_n+'</div></td>';
   h+='<td class="mono">'+(row.kq.qc!=null?row.kq.qc.toFixed(2):'—')+'<div class="hint">'+row.kq.qcn+' '+T.th_created_n.toLowerCase()+'</div></td>';
   h+='<td class="mono">'+(row.kq.qe!=null?row.kq.qe.toFixed(2):'—')+'<div class="hint">'+row.kq.qen+' '+T.th_expanded_n.toLowerCase()+'</div></td>';
   if(HAS_F)h+='<td><span class="mono" style="font-weight:700;color:var(--gold);font-size:15px">'+(row.score!=null?row.score.toFixed(2):'—')+'</span></td>';
   h+='<td class="mono" style="font-size:12px">'+(row.total?row.done+' / '+row.total:'—')
    +(row.mine&&!IS_JURY?'<div class="hint">'+T.my_assigned+': '+row.mineDone+' / '+row.mine+'</div>':'')+'</td>';
  }
  h+='<td>'+(QUAL?'<button class="btn-s" style="padding:4px 10px;font-size:12px" title="'+esc(T.tip_articles_btn)+'" onclick="toggleArt('+i+')">'+T.articles_btn+(row.r?' ('+(IS_JURY?row.mine:row.r.articles)+')':'')+'</button>':'')+'</td></tr>';
  if(QUAL&&ARTOPEN===row.user){
   h+='<tr><td colspan="99" style="background:var(--cdx-neutral)"><div style="max-width:1000px">';
   if(legacyDone(row.user)){
    const lg=LEGACY[row.user],lv=legacyVals(row.user);
    h+='<div style="margin-bottom:10px;padding:10px 12px;border-radius:2px;background:var(--cdx-ok-subtle)"><b>✓ '+T.legacy_title+'</b>'
     +(lv.length?' · '+T.th_avg+': '+(lv.reduce((x,y)=>x+y,0)/lv.length).toFixed(2)+' ('+lv.length+' '+T.scores_n+')':'');
    (lg.comments||[]).forEach(c=>{h+='<div style="padding:3px 0"><b>'+esc(c.jury)+'</b> <span class="hint mono">'+esc(c.date)+'</span> '+esc(c.text)+'</div>'});
    h+='<div class="hint" style="margin-top:4px">'+T.legacy_h+'</div></div>';
   }
   const items=[];
   projects.forEach(p=>{const pp=row.r?row.r.perProject[p.host]:null;
    (pp&&pp.list?pp.list:[]).filter(inScope).filter(it=>!IS_JURY||isMine(row.user,it.title)).forEach(it=>items.push({...it,origin:p.origin,label:p.label,host:p.host,api:p.api}))});
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
    const btn=(cls,f,cur,lbl)=>'<button class="btn-s '+cls+'" data-f="'+f+'" title="'+esc(T['tip_'+(cls==='art-filt-btn'?'af_':'rf_')+f]||'')+'" style="padding:3px 10px;font-size:12px'+(cur===f?';background:var(--grad-dark);color:var(--cdx-on-prog)':'')+'">'+esc(lbl)+'</button>';
    if(SCOPE==='all'){
     h+='<div style="display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap">';
     h+=btn('art-filt-btn','all',ART_FILTER,T.filter_all+' ('+items.length+')');
     h+=btn('art-filt-btn','created',ART_FILTER,T.filter_created+' ('+nC+')');
     h+=btn('art-filt-btn','expanded',ART_FILTER,T.filter_expanded+' ('+nE+')');
     h+='</div>';
    }
    h+='<div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">';
    h+=btn('rev-filt-btn','all',REV_FILTER,T.filter_all);
    h+=btn('rev-filt-btn','todo',REV_FILTER,T.filter_todo+' ('+(items.length-nDone)+')');
    h+=btn('rev-filt-btn','done',REV_FILTER,T.filter_done+' ('+nDone+')');
    if(!IS_JURY)h+=btn('rev-filt-btn','mine',REV_FILTER,T.filter_mine+' ('+nMine+')');
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
      h+='<div class="art-card" title="'+esc(T.tip_card)+'" style="'+(rv?'opacity:.72':'')+'" onclick="openIdx('+k+')" role="button" tabindex="0" onkeydown="kbd(event,function(){openIdx('+k+')})">';
      h+=thumbHtml;
      h+='<div class="body"><div class="art-title">'+esc(it.title)+'</div>';
      h+='<div class="hint" style="margin-top:4px">'+esc(it.label)+' · +'+fmtN(it.bytes)+(LANG==='uk'?' Б':' B')+'</div>';
      h+='<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:5px">';
      h+='<span class="chip" style="font-size:12px;background:'+(it.created?'var(--cdx-ok-subtle)':'var(--acc-soft)')+';color:'+(it.created?'var(--cdx-ok-text)':'var(--acc)')+'">'+(it.created?T.created:T.expanded)+'</span>';
      h+='<span class="chip '+(rv?'st-ok':'st-todo')+'" style="font-size:12px">'+(rv?'✓ '+T.reviewed:'● '+T.unreviewed)+'</span>';
      if(CONTEST.opt_refs&&it.refs!=null)h+='<span class="chip" style="font-size:12px" title="'+esc(T.tip_chip_refs)+'">'+T.chip_refs+' +'+it.refs+'</span>';
      if(CONTEST.opt_images&&it.images!=null)h+='<span class="chip" style="font-size:12px" title="'+esc(T.tip_chip_images)+'">'+T.chip_images+' +'+it.images+'</span>';
      if(it.st)h+='<span class="chip" style="font-size:12px;background:var(--cdx-warn-subtle);color:var(--cdx-warn)">'+(it.st==='featured'?T.status_featured:(it.st==='list'?T.status_list:T.status_good))+'</span>';
      if(mine)h+='<span class="chip tag-a" style="font-size:12px">'+T.mine+'</span>';
      if(avg!=null)h+='<span class="chip" style="font-size:12px;background:var(--cdx-warn-subtle);color:var(--cdx-warn)">★ '+avg.toFixed(1)+'</span>';
      if(nCm)h+='<span class="hint" style="">💬'+nCm+'</span>';
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
 if(hiddenN&&MIN_BYTES>0)h+='<div class="hint" style="padding:8px 12px">'+T.hidden_min+' ('+fmtN(MIN_BYTES)+BU+'): '+hiddenN+'</div>';
 if(MIN_EXP>0||MIN_NEW>0){
  let hiddenE=0;
  rows.forEach(rw=>{if(rw.r)Object.values(rw.r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{if((it.bytes||0)<(it.created?MIN_NEW:MIN_EXP))hiddenE++}))});
  h+='<div class="hint" style="padding:8px 12px">'+T.hidden_exp+' ('+fmtN(MIN_NEW)+' / '+fmtN(MIN_EXP)+BU+'): '+hiddenE+'</div>';
 }
 el.innerHTML=h;
 const tb=document.getElementById('topbox');
 if(tb){if(QUAL&&!IS_JURY){tb.style.display='block';tb.innerHTML=topHtml(rows)}else{tb.style.display='none';tb.innerHTML=''}}
 const nc=el.querySelector('.carousel');if(nc&&keepScroll)nc.scrollLeft=keepScroll;
 el.querySelectorAll('.art-filt-btn').forEach(b=>{b.onclick=function(){setArtFilter(this.dataset.f)}});
 el.querySelectorAll('.rev-filt-btn').forEach(b=>{b.onclick=function(){setRevFilter(this.dataset.f)}});
 document.getElementById('csvlink').href='/export/'+CID+'.csv?quant='+(QUANT?1:0)+'&qual='+(QUAL?1:0)+(SORT_KEY?'&sort='+SORT_KEY:'');
 renderLoad();renderStatusBox();renderPatrolBox();
 renderModal();
}
window.addEventListener('DOMContentLoaded',()=>{render();if(AUTORUN&&!IS_JURY&&CONTEST)runCheck()});
</script>
{% endif %}
</body></html>"""

ADMIN_TPL = """
<div class="mode" style="margin-bottom:14px">
 <button class="on" onclick="document.getElementById('sec-c').style.display='block';document.getElementById('sec-u').style.display='none';document.getElementById('sec-p').style.display='none';[...this.parentNode.children].forEach(b=>b.className='');this.className='on'" title="{{t.tip_tab_c}}">{{t.tab_c}}</button>
 <button onclick="document.getElementById('sec-c').style.display='none';document.getElementById('sec-u').style.display='block';document.getElementById('sec-p').style.display='none';[...this.parentNode.children].forEach(b=>b.className='');this.className='on'" title="{{t.tip_tab_u}}">{{t.tab_u}}</button>
 <button onclick="document.getElementById('sec-c').style.display='none';document.getElementById('sec-u').style.display='none';document.getElementById('sec-p').style.display='block';[...this.parentNode.children].forEach(b=>b.className='');this.className='on'" title="{{t.tip_tab_p}}">{{t.tab_p}}</button>
</div>

<div id="sec-c" style="display:grid;gap:16px;grid-template-columns:minmax(320px,1fr) minmax(280px,360px)">
 <div class="card">
  <h2 style="margin-top:0">{{t.edit_c if edit_contest else t.new_c}}</h2>
  <form method="post" action="{{url_for('contest_save')}}">
   <input type="hidden" name="id" value="{{edit_contest.id if edit_contest else ''}}">
   <label class="lbl" title="{{t.tip_c_name}}" for="f_name_3">{{t.c_name}}</label>
   <input id="f_name_3" name="name" value="{{edit_contest.name if edit_contest else ''}}" placeholder="Cultural Heritage 2026" required>
   <label class="lbl" title="{{t.tip_projects}}" for="f_projects_4">{{t.projects}}</label>
   <textarea id="f_projects_4" name="projects" rows="3" class="mono" style="font-size:12px">{{'\n'.join(edit_contest.projects) if edit_contest else 'https://uk.wikivoyage.org/wiki/Головна_сторінка\nhttps://uk.wikiquote.org/wiki/Головна_сторінка'}}</textarea>
   {{hint_help(t.projects_h)}}
   <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
    <div><label class="lbl" title="{{t.tip_dstart}}" for="start">{{t.d_start}}</label><input type="date" id="start" name="start" value="{{edit_contest.start if edit_contest else ''}}"></div>
    <div><label class="lbl" title="{{t.tip_dend}}" for="end">{{t.d_end}}</label><input type="date" id="end" name="end" value="{{edit_contest.end if edit_contest else ''}}"></div>
   </div>
   <div style="margin-top:12px;background:var(--acc-soft);border-radius:2px;padding:10px 12px">
    <label class="lbl" style="margin-top:0" title="{{t.tip_tpl}}" for="tpl">{{t.tpl}}</label>
    <input id="tpl" name="template" class="mono" style="font-size:12px"
     value="{{edit_contest.template if edit_contest else ''}}"
     placeholder="https://uk.wikivoyage.org/wiki/Шаблон:Cultural_Heritage_and_Notable_Personalities_2026">
    <label class="hint" title="{{t.tip_talk}}" style="display:flex;align-items:center;gap:6px;margin-top:8px;cursor:pointer">
     <input type="checkbox" id="tplTalkOnly" name="tpl_talk_only" style="width:auto"
      {{'checked' if edit_contest and edit_contest.tpl_talk_only else ''}}> {{t.tpl_talk_only}}
    </label>
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center">
     <button type="button" id="extbtn" class="btn-p" title="{{t.tip_tpl_btn}}" onclick="extractParticipants()">{{t.tpl_btn}}</button>
     <label class="hint" title="{{t.tip_replace}}" style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" id="tplReplace" checked style="width:auto"> {{t.tpl_replace}}</label>
     <span id="extmsg" class="hint mono"></span>
    </div>
    <div id="extbar" style="margin-top:6px"></div>
    {{hint_help(t.tpl_h, 'margin-top:6px')}}
   </div>
   <label class="lbl" title="{{t.tip_parts}}" for="parts">{{t.parts}}</label>
   <textarea id="parts" name="participants" rows="6">{{'\n'.join(edit_contest.participants) if edit_contest else ''}}</textarea>
   <label class="lbl" title="{{t.tip_scope}}" for="f_review_scope_5">{{t.review_scope}}</label>
   <select id="f_review_scope_5" name="review_scope">
    {% for k in ['all','created','expanded'] %}<option value="{{k}}" {{'selected' if (edit_contest.review_scope if edit_contest else 'all')==k else ''}}>{{t['scope_'~k]}}</option>{% endfor %}
   </select>
   {{hint_help(t.scope_h)}}
   <div style="display:flex;flex-direction:column;gap:4px;margin-top:6px">
    <label class="hint" title="{{t.tip_scope_refs}}" style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" name="scope_refs" style="width:auto" {{'checked' if edit_contest and edit_contest.scope_refs else ''}}> {{t.scope_refs}}</label>
    <label class="hint" title="{{t.tip_scope_images}}" style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" name="scope_images" style="width:auto" {{'checked' if edit_contest and edit_contest.scope_images else ''}}> {{t.scope_images}}</label>
   </div>
   {{hint_help(t.scope_extra_h)}}
   <label class="lbl" title="{{t.tip_min_bytes}}" for="f_min_bytes_6">{{t.min_bytes}}</label>
   <input id="f_min_bytes_6" type="number" name="min_bytes" min="0" step="1" style="max-width:160px" title="{{t.tip_min_bytes}}" value="{{(edit_contest.min_bytes or 0) if edit_contest else 0}}">
   {{hint_help(t.min_bytes_h)}}
   <label class="lbl" title="{{t.tip_min_expand}}" for="f_min_expand_7">{{t.min_expand}}</label>
   <input id="f_min_expand_7" type="number" name="min_expand" min="0" step="1" style="max-width:160px" title="{{t.tip_min_expand}}" value="{{(edit_contest.min_expand or 0) if edit_contest else 0}}">
   {{hint_help(t.min_expand_h)}}
   <label class="lbl" title="{{t.tip_min_created}}" for="f_min_created_8">{{t.min_created}}</label>
   <input id="f_min_created_8" type="number" name="min_created" min="0" step="1" style="max-width:160px" title="{{t.tip_min_created}}" value="{{(edit_contest.min_created or 0) if edit_contest else 0}}">
   {{hint_help(t.min_created_h)}}
   {% if edit_contest %}
   <div id="minExpWarn" class="hint" style="display:none;margin-top:6px;padding:8px 10px;border-radius:8px"></div>
   <script>
   (function(){
    var CID_E={{edit_contest.id|tojson}},MSG={warn:{{t.me_warn|tojson}},ok:{{t.me_ok|tojson}},confirm:{{t.me_confirm|tojson}},more:{{t.me_more|tojson}}};
    var inpB=document.querySelector('input[name=min_bytes]'),inp=document.querySelector('input[name=min_expand]'),inpN=document.querySelector('input[name=min_created]'),box=document.getElementById('minExpWarn'),sel=document.querySelector('select[name=review_scope]');
    var form=inp.form,cnt=0,tm=null,seq=0;
    function esc(x){return String(x).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
    function check(){
     var my=++seq,v=inp.value||'0';
     fetch('/api/min_expand_preview/'+encodeURIComponent(CID_E)+'?value='+encodeURIComponent(v)+'&value_new='+encodeURIComponent(inpN.value||'0')+'&value_bytes='+encodeURIComponent(inpB.value||'0')+'&scope='+encodeURIComponent(sel?sel.value:'')).then(function(r){return r.json()}).then(function(d){
      if(my!==seq||d.error)return;
      cnt=d.count;box.style.display='block';
      if(!d.count){box.style.background='#e6f4ea';box.textContent=MSG.ok;return}
      box.style.background='#fdecea';
      var h='<b>'+esc(MSG.warn.replace('{n}',d.count))+'</b><ul style="margin:6px 0 0 18px;padding:0">';
      d.items.forEach(function(x){h+='<li>'+esc(x.participant)+' — '+esc(x.article)+' (+'+x.bytes+', '+x.scores+')</li>'});
      if(d.count>d.items.length)h+='<li>'+esc(MSG.more)+' '+(d.count-d.items.length)+'</li>';
      box.innerHTML=h+'</ul>';
     });
    }
    function later(){clearTimeout(tm);tm=setTimeout(check,300)}
    inp.addEventListener('input',later);inpN.addEventListener('input',later);inpB.addEventListener('input',later);if(sel)sel.addEventListener('change',later);
    form.addEventListener('submit',function(e){if(cnt>0&&!confirm(MSG.confirm.replace('{n}',cnt)))e.preventDefault()});
   })();
   </script>
   {% endif %}
   <div style="margin-top:12px;background:var(--acc-soft);border-radius:2px;padding:10px 12px">
    <div class="lbl" style="margin-top:0">{{t.extras_title}}</div>
    <label class="hint" title="{{t.tip_opt_refs}}" style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" name="opt_refs" style="width:auto" {{'checked' if edit_contest and edit_contest.opt_refs else ''}}> {{t.opt_refs}}</label>
    <div style="margin:2px 0 6px 22px"><label class="lbl" style="margin-top:0" title="{{t.tip_src_tag}}" for="f_src_tag_9">{{t.src_tag}}</label><input id="f_src_tag_9" name="src_tag" class="mono" style="font-size:12px;max-width:260px" title="{{t.tip_src_tag}}" placeholder="#1Lib1Ref" value="{{(edit_contest.src_tag or '') if edit_contest else ''}}">{{hint_help(t.src_tag_h)}}</div>
    <label class="hint" title="{{t.tip_opt_images}}" style="display:flex;align-items:center;gap:6px;cursor:pointer;margin-top:4px"><input type="checkbox" name="opt_images" style="width:auto" {{'checked' if edit_contest and edit_contest.opt_images else ''}}> {{t.opt_images}}</label>
    <label class="hint" title="{{t.tip_opt_status}}" style="display:flex;align-items:center;gap:6px;cursor:pointer;margin-top:4px"><input type="checkbox" name="opt_status" style="width:auto" {{'checked' if edit_contest and edit_contest.opt_status else ''}}> {{t.opt_status}}</label>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
     <div><label class="lbl" title="{{t.tip_cat_featured}}" for="f_cat_featured_10">{{t.cat_featured}}</label><input id="f_cat_featured_10" name="cat_featured" class="mono" style="font-size:12px" title="{{t.tip_cat_featured}}" placeholder="Вікіпедія:Вибрані статті" value="{{(edit_contest.cat_featured or '') if edit_contest else ''}}"></div>
     <div><label class="lbl" title="{{t.tip_cat_good}}" for="f_cat_good_11">{{t.cat_good}}</label><input id="f_cat_good_11" name="cat_good" class="mono" style="font-size:12px" title="{{t.tip_cat_good}}" placeholder="Вікіпедія:Добрі статті" value="{{(edit_contest.cat_good or '') if edit_contest else ''}}"></div>
    </div>
    <label class="lbl" title="{{t.tip_cat_list}}" for="f_cat_list_12">{{t.cat_list}}</label><input id="f_cat_list_12" name="cat_list" class="mono" style="font-size:12px" title="{{t.tip_cat_list}}" placeholder="Вікіпедія:Вибрані списки" value="{{(edit_contest.cat_list or '') if edit_contest else ''}}">
    {{hint_help(t.cat_h)}}
   </div>
   <div style="margin-top:12px;background:var(--acc-soft);border-radius:2px;padding:10px 12px">
    <label class="hint" title="{{t.tip_patrol_opt}}" style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" name="patrol" style="width:auto" {{'checked' if edit_contest and edit_contest.patrol else ''}}> <b>{{t.patrol_opt}}</b></label>
    <label class="lbl" title="{{t.tip_patrol_end}}" for="f_patrol_end_13">{{t.patrol_end}}</label>
    <input id="f_patrol_end_13" type="date" name="patrol_end" title="{{t.tip_patrol_end}}" value="{{(edit_contest.patrol_end or '') if edit_contest else ''}}">
    {{hint_help(t.patrol_opt_h)}}
   </div>
   <div class="lbl" id="juryLbl" title="{{t.tip_cjury}}">{{t.contest_jury}}</div>
   <div role="group" aria-labelledby="juryLbl" style="display:flex;flex-wrap:wrap;gap:6px 14px;border:1px solid var(--line);border-radius:2px;padding:8px 10px;background:var(--cdx-base)">
    {% for u in users if u.role=='jury' %}
    <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px" title="{{t.tip_cjury}}"><input type="checkbox" name="jury" value="{{u.login}}" style="width:auto" {{'checked' if edit_contest and u.login in (edit_contest.jury or []) else ''}}> {{u.name}} <span class="mono hint">({{u.login}})</span></label>
    {% else %}<span class="hint">{{t.no_jury_accounts}}</span>{% endfor %}
   </div>
   {{hint_help(t.contest_jury_h)}}
   <label class="lbl" title="{{t.tip_formula}}" for="f_formula_14">{{t.formula}}</label>
   <input id="f_formula_14" name="formula" class="mono" title="{{t.tip_formula}}" placeholder="bytes / 1000 + quality * 10" value="{{(edit_contest.formula or '') if edit_contest else ''}}">
   {{hint_help(t.formula_h)}}
   {% if msg %}<div style="color:var(--ok);font-weight:600;margin-top:8px">{{msg}}</div>{% endif %}
   {% if edit_contest %}{{hint_help(t.upd_h, 'margin-top:10px')}}{% endif %}
   <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
    <button class="btn-p" title="{{t.tip_save}}">{{t.save if edit_contest else t.create}}</button>
    {% if edit_contest %}<button class="btn-p" name="after" value="check" title="{{t.tip_save_update}}">{{t.save_update}}</button>{% endif %}
    {% if edit_contest %}<a href="?view=admin" class="btn-s" style="text-decoration:none;padding:8px 14px">{{t.cancel}}</a>{% endif %}
   </div>
  </form>
 </div>
 <div class="card">
  <h2 style="margin-top:0">{{t.tab_c}} ({{contests|length}})</h2>
  {% if not contests %}<div class="hint">{{t.no_c}}</div>{% endif %}
  {% for c in contests %}
  <div style="border-bottom:1px solid var(--line);padding:10px 0">
   <div style="font-weight:700">{{c.name}}</div>
   <div class="hint">{{c.hosts}} · {{t.parts_n}}: {{c.participants|length}} · {{t.jury}}: {{c.jury|length}}</div>
   <div class="mono hint">{{c.formula}}</div>
   <div style="display:flex;gap:6px;margin-top:4px">
    <a class="btn-s" style="padding:4px 10px;font-size:12px;text-decoration:none" title="{{t.tip_edit}}" href="?view=admin&edit={{c.id}}">{{t.edit}}</a>
    <form method="post" action="{{url_for('contest_delete')}}" style="margin:0">
     <input type="hidden" name="id" value="{{c.id}}">
     <button class="btn-d" title="{{t.tip_delete}}" style="padding:4px 10px;font-size:12px">{{t.delete}}</button></form>
   </div>
  </div>
  {% endfor %}
 </div>
</div>

<div id="sec-u" style="display:none">
 <div style="display:grid;gap:16px;grid-template-columns:minmax(300px,420px) 1fr">
  <div class="card"><h2 style="margin-top:0">{{t.add_u}}</h2>
   <form method="post" action="{{url_for('user_save')}}">
    <label class="lbl" title="{{t.tip_role}}" for="f_role_15">{{t.role}}</label>
    <select id="f_role_15" name="role"><option value="jury">{{t.jury}}</option><option value="admin">{{t.admin}}</option></select>
    <label class="lbl" title="{{t.tip_uname}}" for="f_name_16">{{t.u_name}}</label><input id="f_name_16" name="name">
    <label class="lbl" title="{{t.tip_ulogin}}" for="f_login_17">{{t.login}}</label><input id="f_login_17" name="login" required>
    <label class="lbl" title="{{t.tip_upass}}" for="f_password_18">{{t.password}}</label><input id="f_password_18" name="password" type="password" required>
    <button class="btn-p" title="{{t.tip_create_u}}" style="margin-top:12px">{{t.create}}</button>
   </form></div>
  <div class="card"><h2 style="margin-top:0">{{t.accounts}} ({{users|length}})</h2>
   {% for u in users %}
   <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)">
    <span class="chip {{'tag-a' if u.role=='admin' else 'tag-j'}}">{{t[u.role]}}</span>
    <b>{{u.name}}</b><span class="mono hint">{{u.login}}</span>
    {% if u.login!=me.login %}
    <form method="post" action="{{url_for('user_delete')}}" style="margin-left:auto">
     <input type="hidden" name="login" value="{{u.login}}">
     <button class="btn-d" title="{{t.tip_del_user}}" style="padding:3px 10px;font-size:12px">{{t.delete}}</button></form>
    {% else %}<span class="hint" style="margin-left:auto">{{t.its_you}}</span>{% endif %}
   </div>{% endfor %}</div>
 </div>
</div>

<div id="sec-p" style="display:none">
 <div class="card" style="max-width:400px"><h2 style="margin-top:0">{{t.ch_pass}} — {{me.name}}</h2>
  <form method="post" action="{{url_for('change_password')}}">
   <label class="lbl" for="f_p1_19">{{t.new_pass}}</label><input id="f_p1_19" name="p1" type="password" required minlength="4">
   <label class="lbl" for="f_p2_20">{{t.again}}</label><input id="f_p2_20" name="p2" type="password" required minlength="4">
   <button class="btn-p" style="margin-top:12px">{{t.save}}</button>
  </form></div>
</div>
"""

ANALYSIS_TPL = """
{% if not contest %}
 <div class="card hint">{{t.no_c}}</div>
{% else %}
<div class="card" style="display:flex;flex-wrap:wrap;gap:14px;align-items:center">
 {% if me.role=='admin' %}
 <div>
  <div class="lbl" style="margin:0">{{t.mode}}</div>
  <div class="mode" style="margin-top:4px">
   <button id="mq" class="on" title="{{t.tip_quant}}" onclick="setMode('quant')">{{t.quant}}</button>
   <button id="ml" title="{{t.tip_qual}}" onclick="setMode('qual')">{{t.qual}}</button>
   {% if contest and contest.opt_status %}<button id="ms" title="{{t.tip_mode_status}}" onclick="toggleBox('status')">{{t.mode_status}}</button>{% endif %}
   {% if contest and contest.patrol %}<button id="mp" title="{{t.tip_mode_patrol}}" onclick="toggleBox('patrol')">{{t.mode_patrol}}</button>{% endif %}
  </div>
  <div id="modehint" class="hint" style="margin-top:4px">{{t.modes_h}}</div>
 </div>
 {% endif %}
 <div style="margin-left:auto;text-align:right;min-width:280px">
  <div style="display:flex;gap:8px;justify-content:flex-end">
   <a id="csvlink" title="{{t.tip_export}}" class="btn-s" style="text-decoration:none;padding:8px 14px" href="/export/{{cid}}.csv?quant=1&qual=0">{{t.export}}</a>
   {% if me.role=='admin' %}
   <button id="runbtn" class="btn-p" title="{{t.tip_check}}" onclick="runCheck()">{{t.check}}</button>
   {% else %}
   <span class="hint" style="align-self:center;max-width:200px;text-align:right">{{t.jury_no_check}}</span>
   {% endif %}
  </div>
  <div id="runbar" style="margin-top:6px"></div>
 </div>
</div>
<div id="statusbox" class="card" style="display:none"></div>
<div id="patrolbox" class="card" style="display:none"></div>
<div id="loadbox" class="card" style="display:none">
 <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
  <div><div class="lbl" style="margin:0">{{t.load_title if me.role=='admin' else t.my_progress_title}}</div>
   {% if me.role=='admin' %}{{hint_help(t.distribute_h)}}{% endif %}</div>
  {% if me.role=='admin' %}
  <div style="margin-left:auto;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
   <div><label class="lbl" style="margin:0 0 2px" for="distPer">{{t.per_article}}</label><input id="distPer" title="{{t.tip_per_article}}" type="number" min="1" value="1" style="width:70px"></div>
   <label class="hint" title="{{t.tip_reset}}" style="display:flex;align-items:center;gap:6px;margin-bottom:8px"><input type="checkbox" id="distReset" style="width:auto"> {{t.reset_dist}}</label>
   <button class="btn-p" title="{{t.tip_distribute}}" onclick="runDistribute()">{{t.distribute}}</button>
  </div>
  {% endif %}
 </div>
 {% if me.role=='admin' %}
 <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
  <div class="lbl" style="margin:0" title="{{t.tip_reassign}}">{{t.reassign_title}}</div>
  {{hint_help(t.reassign_h)}}
  <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-top:6px">
   <div><label class="lbl" style="margin:0 0 2px" for="reFrom">{{t.reassign_from}}</label>
    <select id="reFrom" title="{{t.tip_reassign}}"><option value=""></option>{% for u in users if u.role=='jury' %}<option value="{{u.login}}">{{u.name}} ({{u.login}})</option>{% endfor %}</select></div>
   <div><label class="lbl" style="margin:0 0 2px" for="reTo">{{t.reassign_to}}</label>
    <select id="reTo" title="{{t.tip_reassign}}"><option value=""></option>{% for u in users if u.role=='jury' and u.login in (contest.jury or []) %}<option value="{{u.login}}">{{u.name}} ({{u.login}})</option>{% endfor %}</select></div>
   <button class="btn-p" title="{{t.tip_reassign}}" onclick="runReassign()">{{t.reassign_btn}}</button>
  </div>
 </div>
 {% endif %}
 <div id="distmsg" class="hint mono" style="margin-top:6px"></div>
 <div id="loadtbl" style="margin-top:8px"></div>
</div>
<div class="card" style="padding:0;overflow-x:auto"><div id="tbl"></div></div>
<div id="topbox" class="card" style="display:none"></div>
<div class="hint">{% if me.role=='admin' %}{{t.f_of}} <span class="chip">{{contest.formula if contest.formula else '—'}}</span> · {% endif %}{{t.scope_label}} <span class="chip">{{t['scope_'~(contest.review_scope or 'all')]}}</span>{% if contest.min_bytes %} · {{t.min_bytes_chip}} <span class="chip">{{contest.min_bytes}} {{t.bytes_unit}}</span>{% endif %}{% if contest.min_created %} · {{t.min_created_chip}} <span class="chip">{{contest.min_created}} {{t.bytes_unit}}</span>{% endif %}{% if contest.min_expand %} · {{t.min_expand_chip}} <span class="chip">{{contest.min_expand}} {{t.bytes_unit}}</span>{% endif %}</div>
{% endif %}
{% if me.role=='jury' %}
<div class="card" style="max-width:400px;margin-top:14px">
 <h2 style="margin-top:0">{{t.my_pass_title}} — {{me.name}}</h2>
 <form method="post" action="{{url_for('change_password')}}">
  <label class="lbl" for="f_p1_21">{{t.new_pass}}</label><input id="f_p1_21" name="p1" type="password" required minlength="4">
  <label class="lbl" for="f_p2_22">{{t.again}}</label><input id="f_p2_22" name="p2" type="password" required minlength="4">
  <button class="btn-p" style="margin-top:12px">{{t.save}}</button>
 </form>
</div>
{% endif %}
"""

# ── Допоміжне ────────────────────────────────────────────────
def patrol_json(cid):
    row = db().execute("SELECT data FROM patrol_results WHERE contest_id=?", (cid,)).fetchone()
    return row["data"] if row else "null"

def get_contests():
    rows = db().execute("SELECT * FROM contests").fetchall()
    cj = {}
    for r in db().execute("SELECT contest_id, login FROM contest_jury ORDER BY login"):
        cj.setdefault(r["contest_id"], []).append(r["login"])
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
        c["jury"] = cj.get(c["id"], [])
        out.append(c)
    return out

def get_assess(cid, only=None):
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
        if only is None or r["jury"] == only:
            slot(r["participant"], r["article"])["reviewed"].append(r["jury"])
    for r in db().execute(
            "SELECT * FROM article_assignments WHERE contest_id=? ORDER BY jury", (cid,)):
        if only is None or r["jury"] == only:
            slot(r["participant"], r["article"])["assigned"].append(r["jury"])
    return assess

def split_stats(r, min_exp=0, min_new=0):
    """Нові та доповнені статті учасника: кількість і додані байти."""
    s = {"created": 0, "expanded": 0, "bc": 0, "be": 0,
         "refs": 0, "images": 0, "good": 0, "featured": 0, "flist": 0}
    for pp in ((r or {}).get("perProject") or {}).values():
        for it in (pp.get("list") or []):
            s["refs"] += it.get("refs") or 0
            s["images"] += it.get("images") or 0
            if it.get("st") == "good":
                s["good"] += 1
            elif it.get("st") == "featured":
                s["featured"] += 1
            elif it.get("st") == "list":
                s["flist"] += 1
            if it.get("created"):
                if (it.get("bytes") or 0) >= min_new:  # замалі нові статті не приймаються
                    s["created"] += 1
                    s["bc"] += it.get("bytes", 0)
            elif (it.get("bytes") or 0) >= min_exp:  # замалі доповнення не приймаються
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

def scope_flags(contest):
    """Додаткові умови «що перевіряє журі»: лише статті з доданими джерелами / зображеннями."""
    return tuple(k for k in ("refs", "images") if (contest or {}).get("scope_" + k))

def _nonneg(v):
    try:
        return max(0, int(float(str(v).replace(",", ".").strip() or 0)))
    except (TypeError, ValueError):
        return 0

def parse_min_expand(v):
    """Невід'ємне ціле з поля форми (порожнє/некоректне = 0 = без обмеження)."""
    return _nonneg(v)

def scope_min(contest):
    """Мінімум доданих байтів, від якого доповнення статті приймається (0 = без обмеження)."""
    return _nonneg((contest or {}).get("min_expand"))

def scope_min_new(contest):
    """Мінімум байтів, від якого нова (започаткована) стаття приймається (0 = без обмеження)."""
    return _nonneg((contest or {}).get("min_created"))

def scope_min_bytes(contest):
    """Мінімальний внесок учасника: сума доданих байтів (0 = без обмеження)."""
    return _nonneg((contest or {}).get("min_bytes"))

def in_scope(it, scope, flags=(), min_exp=0, min_new=0):
    """Чи входить стаття (запис зі списку внеску) до обраного адміном обсягу перевірки.
    Замале доповнення (< min_exp) і замала нова стаття (< min_new) не приймаються."""
    ok = scope == "all" or (scope == "created") == bool(it.get("created"))
    if ok and (it.get("bytes") or 0) < (min_new if it.get("created") else min_exp):
        ok = False
    if ok and "refs" in flags:
        ok = (it.get("refs") or 0) > 0
    if ok and "images" in flags:
        ok = (it.get("images") or 0) > 0
    return ok

def scope_titles(r, scope, flags=(), min_exp=0, min_new=0):
    """Назви статей учасника в обсязі перевірки (None = без обмежень)."""
    if (scope == "all" and not flags and not min_exp and not min_new) or not r:
        return None
    return {it.get("title") for pp in (r.get("perProject") or {}).values()
            for it in (pp.get("list") or []) if in_scope(it, scope, flags, min_exp, min_new)}

def kind_quality(participant_assess, r, scope, flags=(), min_exp=0, min_new=0):
    """Середній бал журі окремо для нових і доповнених статей учасника (у межах обсягу перевірки).
    Повертає {"created": (середнє|None, кількість оцінених статей), "expanded": (...)}."""
    kinds = {"created": [], "expanded": []}
    seen = set()
    for pp in ((r or {}).get("perProject") or {}).values():
        for it in (pp.get("list") or []):
            t = it.get("title")
            if not t or t in seen or not in_scope(it, scope, flags, min_exp, min_new):
                continue
            seen.add(t)
            vals = [v for v in ((participant_assess or {}).get(t, {}).get("scores") or {}).values()
                    if v is not None]
            if vals:
                kinds["created" if it.get("created") else "expanded"].append(sum(vals) / len(vals))
    return {k: ((sum(v) / len(v)) if v else None, len(v)) for k, v in kinds.items()}

def article_quality(participant_assess, legacy=None, allowed=None):
    """Якість учасника = середнє арифметичне середніх оцінок по кожній статті
    (спершу усереднюємо оцінки журі в межах статті, потім — по статтях).
    Якщо оцінок по статтях немає, але є оцінки з попередньої версії — беремо їх середнє."""
    article_avgs = []
    n = 0
    for art, a in (participant_assess or {}).items():
        if allowed is not None and art not in allowed:
            continue
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

def contest_access(cid):
    """Адмін бачить усі конкурси; журі — лише ті, до яких його додано."""
    u = session.get("user")
    if not u:
        return False
    if u["role"] == "admin":
        return True
    return db().execute("SELECT 1 FROM contest_jury WHERE contest_id=? AND login=?",
                        (cid, u["login"])).fetchone() is not None

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
    if me.role != "admin":
        contests = [c for c in contests if me.login in (c.jury or [])]
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
        assess = get_assess(contest.id, None if me.role == "admin" else me.login)
    tpl = MAIN_HTML.replace("{% include 'ADMIN' %}", ADMIN_TPL)\
                   .replace("{% include 'ANALYSIS' %}", ANALYSIS_TPL)
    users = [D(dict(u)) for u in db().execute("SELECT * FROM users").fetchall()]
    return render_template_string(
        tpl, t=D(T[l]), l=l, me=me, contests=contests, cid=cid, contest=contest,
        view=view, edit_contest=edit_contest, users=users,
        msg=request.args.get("msg"),
        autorun=(request.args.get("autorun") == "1" and me.role == "admin"),
        tjson=json.dumps(T[l], ensure_ascii=False),
        cjson=json.dumps({k: v for k, v in contest.items() if me.role == "admin" or k != "jury"},
                         ensure_ascii=False) if contest else "null",
        rjson=json.dumps(results, ensure_ascii=False) if results else "null",
        pjson=(patrol_json(contest["id"]) if (contest and me.role == "admin") else "null"),
        ajson=json.dumps(assess, ensure_ascii=False),
        juryjson=json.dumps([{"login": u.login, "name": u.name, "role": u.role,
                              "inc": u.login in ((contest.jury or []) if contest else [])} for u in users
                             if me.role == "admin" or u.login == me.login],
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
    if formula and not validate_formula(formula):
        formula = ""  # некоректну формулу не зберігаємо; порожня = формула не використовується
    cid = f.get("id") or "c" + hex(int(time.time() * 1000))[2:]
    scope = f.get("review_scope") if f.get("review_scope") in ("all", "created", "expanded") else "all"
    chk = lambda k: 1 if f.get(k) else 0
    min_expand = parse_min_expand(f.get("min_expand"))
    min_created = parse_min_expand(f.get("min_created"))
    min_bytes = parse_min_expand(f.get("min_bytes"))
    scope_refs, scope_images = chk("scope_refs"), chk("scope_images")
    db().execute("""INSERT INTO contests(id,name,template,projects,participants,formula,start,end,
        tpl_talk_only,review_scope,opt_refs,opt_images,opt_status,cat_featured,cat_good,cat_list,src_tag,min_expand,
        scope_refs,scope_images,patrol,patrol_end,min_created,min_bytes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name,template=excluded.template,
        projects=excluded.projects,participants=excluded.participants,
        formula=excluded.formula,start=excluded.start,end=excluded.end,
        tpl_talk_only=excluded.tpl_talk_only,review_scope=excluded.review_scope,
        opt_refs=excluded.opt_refs,opt_images=excluded.opt_images,opt_status=excluded.opt_status,
        cat_featured=excluded.cat_featured,cat_good=excluded.cat_good,cat_list=excluded.cat_list,src_tag=excluded.src_tag,min_expand=excluded.min_expand,
        scope_refs=excluded.scope_refs,scope_images=excluded.scope_images,
        patrol=excluded.patrol,patrol_end=excluded.patrol_end,
        min_created=excluded.min_created,min_bytes=excluded.min_bytes""",
        (cid, f.get("name", "").strip(), f.get("template", "").strip(),
         json.dumps(projects), json.dumps(participants), formula,
         f.get("start", ""), f.get("end", ""), chk("tpl_talk_only"), scope,
         chk("opt_refs") or scope_refs, chk("opt_images") or scope_images, chk("opt_status"),
         f.get("cat_featured", "").strip(), f.get("cat_good", "").strip(),
         f.get("cat_list", "").strip(), f.get("src_tag", "").strip(), min_expand,
         scope_refs, scope_images, chk("patrol"), f.get("patrol_end", "").strip(),
         min_created, min_bytes))
    # журі конкурсу: оновлюємо лише склад; оцінки, перевірки та призначення не чіпаємо
    valid = {r["login"] for r in db().execute("SELECT login FROM users WHERE role='jury'")}
    chosen = [j for j in f.getlist("jury") if j in valid]
    db().execute("DELETE FROM contest_jury WHERE contest_id=?", (cid,))
    db().executemany("INSERT OR IGNORE INTO contest_jury VALUES(?,?)", [(cid, j) for j in chosen])
    # з кешу внеску прибираємо учасників, яких більше немає у списку (оцінки не видаляємо)
    rr = db().execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
    if rr:
        old = json.loads(rr["data"]) or {}
        keep = set(participants)
        kept = {k: v for k, v in old.items() if k in keep}
        if len(kept) != len(old):
            db().execute("UPDATE results SET data=? WHERE contest_id=?",
                         (json.dumps(kept, ensure_ascii=False), cid))
    db().commit()
    if f.get("after") == "check":
        return redirect(url_for("index", view="analysis", contest=cid, autorun=1))
    return redirect(url_for("index", view="admin", contest=cid, msg=T[lang()]["saved"]))

@app.route("/admin/contest/delete", methods=["POST"])
@admin_required
def contest_delete():
    cid = request.form["id"]
    for tbl in ("contests", "results", "scores", "comments", "article_scores",
                "article_comments", "article_reviews", "article_assignments", "contest_jury",
                "patrol_results"):
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
        db().execute("DELETE FROM contest_jury WHERE login=?", (request.form["login"],))
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
@app.route("/api/contest/<cid>/participants", methods=["POST"])
@login_required
def api_set_participants(cid):
    """Зберігає список учасників наявного конкурсу (без створення нового); оцінки не чіпає."""
    if session["user"]["role"] != "admin":
        return jsonify(error="forbidden"), 403
    if not db().execute("SELECT 1 FROM contests WHERE id=?", (cid,)).fetchone():
        return jsonify(error="not_found"), 404
    names = (request.get_json() or {}).get("participants") or []
    participants = list(dict.fromkeys(str(x).strip() for x in names if str(x).strip()))
    db().execute("UPDATE contests SET participants=? WHERE id=?",
                 (json.dumps(participants, ensure_ascii=False), cid))
    rr = db().execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
    if rr:
        keep = set(participants)
        old = json.loads(rr["data"]) or {}
        db().execute("UPDATE results SET data=? WHERE contest_id=?",
                     (json.dumps({k: v for k, v in old.items() if k in keep}, ensure_ascii=False), cid))
    db().commit()
    return jsonify(ok=True, count=len(participants))

@app.route("/api/patrol/<cid>", methods=["POST"])
@login_required
def api_patrol(cid):
    """Зберігає зібрані з журналу патрулювання дані (лише адмін)."""
    if session["user"]["role"] != "admin":
        return jsonify(error="forbidden"), 403
    db().execute("""INSERT INTO patrol_results VALUES(?,?,?)
        ON CONFLICT(contest_id) DO UPDATE SET data=excluded.data,updated=excluded.updated""",
        (cid, json.dumps(request.get_json(), ensure_ascii=False), int(time.time())))
    db().commit()
    return jsonify(ok=True)

@app.route("/api/results/<cid>", methods=["POST"])
@login_required
def api_results(cid):
    if session["user"]["role"] != "admin":
        return jsonify(error="forbidden"), 403
    db().execute("""INSERT INTO results VALUES(?,?,?)
        ON CONFLICT(contest_id) DO UPDATE SET data=excluded.data,updated=excluded.updated""",
        (cid, json.dumps(request.get_json(), ensure_ascii=False), int(time.time())))
    db().commit()
    return jsonify(ok=True)

@app.route("/api/score", methods=["POST"])
@login_required
def api_score():
    d = request.get_json()
    if not contest_access(d["contest"]):
        return jsonify(error="forbidden"), 403
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
    if not contest_access(d["contest"]):
        return jsonify(error="forbidden"), 403
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
    if not contest_access(d["contest"]):
        return jsonify(error="forbidden"), 403
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
    srow = con.execute("SELECT review_scope, scope_refs, scope_images, min_expand, min_created, min_bytes "
                       "FROM contests WHERE id=?", (cid,)).fetchone()
    scope = (srow["review_scope"] if srow else None) or "all"
    flags = scope_flags(dict(srow)) if srow else ()
    min_exp = scope_min(dict(srow)) if srow else 0
    min_new = scope_min_new(dict(srow)) if srow else 0
    min_b = scope_min_bytes(dict(srow)) if srow else 0
    if flags and not any(("refs" in it or "images" in it)
                         for u in results for pp in ((results[u] or {}).get("perProject") or {}).values()
                         for it in (pp.get("list") or [])):
        return jsonify(error="no_extras"), 400  # показники джерел/зображень ще не пораховано
    for user in sorted(results):
        if user in legacy and (legacy[user]["scores"] or legacy[user]["comments"]):
            continue  # внесок уже перевірено в попередній версії
        if ((results[user] or {}).get("bytes") or 0) < min_b:
            continue  # внесок нижчий за мінімальний поріг
        for pp in ((results[user] or {}).get("perProject") or {}).values():
            for it in (pp.get("list") or []):
                if not in_scope(it, scope, flags, min_exp, min_new):
                    continue
                k = (user, it.get("title", ""))
                if k[1] and k not in seen:
                    seen.add(k)
                    items.append(k)
    if not items:
        return jsonify(error="no_results"), 400
    pool = [r["login"] for r in con.execute(
        "SELECT cj.login FROM contest_jury cj JOIN users u ON u.login=cj.login "
        "WHERE cj.contest_id=? AND u.role='jury' ORDER BY cj.login", (cid,))]
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

@app.route("/api/reassign/<cid>", methods=["POST"])
@login_required
def api_reassign(cid):
    """Передає НЕПЕРЕВІРЕНІ статті одного журі іншому (лише адмін).
    Перевірені статті, оцінки, коментарі та позначки не змінюються."""
    if session["user"]["role"] != "admin":
        return jsonify(error="forbidden"), 403
    d = request.get_json(silent=True) or {}
    src, dst = d.get("from"), d.get("to")
    con = db()
    if not src or not dst or src == dst:
        return jsonify(error="bad_request"), 400
    ok = {r["login"] for r in con.execute(
        "SELECT cj.login FROM contest_jury cj JOIN users u ON u.login=cj.login "
        "WHERE cj.contest_id=? AND u.role='jury'", (cid,))}
    if dst not in ok:
        return jsonify(error="bad_request"), 400
    if not con.execute("SELECT 1 FROM users WHERE login=? AND role='jury'", (src,)).fetchone():
        return jsonify(error="bad_request"), 400
    nm = con.execute("SELECT name FROM users WHERE login=?", (src,)).fetchone()
    src_name = (nm["name"] if nm else None) or src
    # що вже зроблено джерелом: перевірки, оцінки, коментарі (статті) та оцінки/коментарі рівня учасника
    done = set()
    for r in con.execute("SELECT participant, article FROM article_reviews "
                         "WHERE contest_id=? AND jury=?", (cid, src)):
        done.add((r["participant"], r["article"]))
    for r in con.execute("SELECT participant, article FROM article_scores "
                         "WHERE contest_id=? AND jury=?", (cid, src)):
        done.add((r["participant"], r["article"]))
    for r in con.execute("SELECT participant, article FROM article_comments "
                         "WHERE contest_id=? AND jury_name=?", (cid, src_name)):
        done.add((r["participant"], r["article"]))
    legacy_users = set()
    for r in con.execute("SELECT participant FROM scores WHERE contest_id=? AND jury=? "
                         "AND value IS NOT NULL", (cid, src)):
        legacy_users.add(r["participant"])
    for r in con.execute("SELECT participant FROM comments WHERE contest_id=? AND jury_name=?",
                         (cid, src_name)):
        legacy_users.add(r["participant"])
    rows = con.execute("SELECT participant, article FROM article_assignments "
                       "WHERE contest_id=? AND jury=?", (cid, src)).fetchall()
    movable, kept = [], 0
    for r in rows:
        k = (r["participant"], r["article"])
        if k in done or r["participant"] in legacy_users:
            kept += 1
        else:
            movable.append(k)
    if d.get("dry"):
        return jsonify(ok=True, movable=len(movable), kept=kept)
    moved = merged = 0
    for p, a in movable:
        has = con.execute("SELECT 1 FROM article_assignments WHERE contest_id=? AND participant=? "
                          "AND article=? AND jury=?", (cid, p, a, dst)).fetchone()
        if has:
            merged += 1  # ця стаття вже призначена одержувачу — просто знімаємо з джерела
        else:
            con.execute("INSERT INTO article_assignments VALUES(?,?,?,?)", (cid, p, a, dst))
            moved += 1
        con.execute("DELETE FROM article_assignments WHERE contest_id=? AND participant=? "
                    "AND article=? AND jury=?", (cid, p, a, src))
    con.commit()
    return jsonify(ok=True, moved=moved, merged=merged, kept=kept)

@app.route("/api/min_expand_preview/<cid>")
@login_required
def api_min_expand_preview(cid):
    """Які вже оцінені статті (доповнення й нові) випадуть з перевірки, якщо задати нові мінімуми (лише адмін).
    Порівнюємо з тим, що діє зараз (збережені обсяг і поріг)."""
    if session["user"]["role"] != "admin":
        return jsonify(error="forbidden"), 403
    con = db()
    crow = con.execute("SELECT review_scope, scope_refs, scope_images, min_expand, min_created, min_bytes "
                       "FROM contests WHERE id=?", (cid,)).fetchone()
    if not crow:
        return jsonify(error="not_found"), 404
    old_scope = crow["review_scope"] or "all"
    flags = scope_flags(dict(crow))
    old_min = crow["min_expand"] or 0
    old_new = crow["min_created"] or 0
    old_b = crow["min_bytes"] or 0
    new_b = parse_min_expand(request.args.get("value_bytes")) if "value_bytes" in request.args else old_b
    new_min = parse_min_expand(request.args.get("value"))
    new_new = parse_min_expand(request.args.get("value_new")) if "value_new" in request.args else old_new
    new_scope = request.args.get("scope") if request.args.get("scope") in ("all", "created", "expanded") else old_scope
    rr = con.execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
    results = json.loads(rr["data"]) if rr else {}
    scored = {}
    for r in con.execute("SELECT participant, article, COUNT(*) n FROM article_scores "
                         "WHERE contest_id=? AND value IS NOT NULL GROUP BY participant, article", (cid,)):
        scored[(r["participant"], r["article"])] = r["n"]
    items, seen = [], set()
    for user, res in (results or {}).items():
        ub = (res or {}).get("bytes") or 0
        for pp in ((res or {}).get("perProject") or {}).values():
            for it in (pp.get("list") or []):
                k = (user, it.get("title", ""))
                if k in seen or k not in scored:
                    continue
                old_ok = ub >= old_b and in_scope(it, old_scope, flags, old_min, old_new)
                new_ok = ub >= new_b and in_scope(it, new_scope, flags, new_min, new_new)
                if old_ok and not new_ok:
                    seen.add(k)
                    items.append({"participant": user, "article": k[1],
                                  "bytes": it.get("bytes", 0), "scores": scored[k]})
    items.sort(key=lambda x: (x["participant"], x["article"]))
    return jsonify(count=len(items), items=items[:15])

@app.route("/api/state/<cid>")
@login_required
def api_state(cid):
    if not contest_access(cid):
        return jsonify(error="forbidden"), 403
    return jsonify(assess=get_assess(cid, None if session["user"]["role"] == "admin"
                                     else session["user"]["login"]))

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
    if not contest_access(cid):
        return "Forbidden", 403
    rr = db().execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
    results = json.loads(rr["data"]) if rr else {}
    assess = get_assess(cid)
    legacy = get_legacy(cid)
    scope = contest.get("review_scope") or "all"
    flags = scope_flags(contest)
    min_exp = scope_min(contest)
    min_new = scope_min_new(contest)
    min_b = scope_min_bytes(contest)
    sort_key = request.args.get("sort") if request.args.get("sort") in ("qc", "qe") else None
    show_score = bool((contest.get("formula") or "").strip()) and session["user"]["role"] == "admin"
    hosts = []
    for p in contest["projects"]:
        m = re.match(r"https?://([^/]+)", p)
        if m:
            hosts.append(m.group(1))

    rows = []
    for user in contest["participants"]:
        r = results.get(user)
        if r and (r.get("bytes") or 0) < min_b:
            continue  # нижче мінімального порогу
        a = assess.get(user, {})
        allowed = scope_titles(r, scope, flags, min_exp, min_new)
        quality, n = article_quality(a, legacy.get(user), allowed)
        kq = kind_quality(a, r, scope, flags, min_exp, min_new)
        sp = split_stats(r, min_exp, min_new)
        score = eval_formula(contest["formula"],
                             {"bytes": r["bytes"], "edits": r["edits"],
                              "articles": r["articles"], "quality": quality,
                              "created": sp["created"], "expanded": sp["expanded"],
                              "refs": sp["refs"], "images": sp["images"],
                              "good": sp["good"], "featured": sp["featured"],
                              "flist": sp["flist"]}) if r else None
        all_comments = [f"[{t['legacy_title']}] {c['jury']} ({c['date']}): {c['text']}"
                        for c in (legacy.get(user) or {}).get("comments", [])]
        for art, ad in a.items():
            if allowed is not None and art not in allowed:
                continue
            for c in ad.get("comments", []):
                all_comments.append(f"[{art}] {c['jury']} ({c['date']}): {c['text']}")
        rows.append((user, r, all_comments, quality, n, score, kq))
    if sort_key and qual:
        kind = "created" if sort_key == "qc" else "expanded"
        rows.sort(key=lambda x: -(x[6][kind][0] if x[6][kind][0] is not None else -1e18))
    else:
        rows.sort(key=lambda x: -(x[5] if (qual and x[5] is not None)
                                  else (x[1]["bytes"] if x[1] else -1)))

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=",")
    head = ["#", t["th_u"]]
    if quant:
        head += [t["th_b"]] + hosts + [t["th_e"], t["th_split"], t["th_split_b"]]
        if contest.get("opt_refs"):
            head.append(t["th_refs"])
        if contest.get("opt_images"):
            head.append(t["th_images"])
        if contest.get("opt_status"):
            head.append(t["th_status"])
    if qual:
        head += [t["th_avg"], t["th_avg_c"], t["th_avg_e"]] + ([t["th_score"]] if show_score else []) + [t["comments"]]
    w.writerow(head)
    for i, (user, r, all_comments, quality, n, score, kq) in enumerate(rows, 1):
        line = [i, user]
        if quant:
            line.append(r["bytes"] if r else "")
            for h in hosts:
                pp = (r or {}).get("perProject", {}).get(h)
                line.append(pp.get("bytes", t["err"]) if pp else "")
            line.append(f"{r['edits']} / {r['articles']}" if r else "")
            sp = split_stats(r, min_exp, min_new) if r else None
            line.append(f"{sp['created']} / {sp['expanded']}" if sp else "")
            line.append(f"{sp['bc']} / {sp['be']}" if sp else "")
            if contest.get("opt_refs"):
                line.append(sp["refs"] if sp else "")
            if contest.get("opt_images"):
                line.append(sp["images"] if sp else "")
            if contest.get("opt_status"):
                line.append(f"{sp['featured']} / {sp['good']} / {sp['flist']}" if sp else "")
        if qual:
            line.append(round(quality, 2) if n else "")
            line.append(round(kq["created"][0], 2) if kq["created"][0] is not None else "")
            line.append(round(kq["expanded"][0], 2) if kq["expanded"][0] is not None else "")
            if show_score:
                line.append(round(score, 2) if score is not None else "")
            line.append(" | ".join(all_comments))
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
