# -*- coding: utf-8 -*-
"""
Тести для WikiRank (app.py).
Запуск:  pytest test_app.py -v
Покриття: формула, авторизація, ролі, конкурси, користувачі,
оцінки, коментарі, результати, CSV-експорт, i18n.
"""
import json
import os
import tempfile

import pytest

# окрема тимчасова БД для кожного запуску тестів
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["WIKIRANK_DB"] = _db_path
os.environ["SECRET_KEY"] = "test-secret"

import app as wr  # noqa: E402  (імпорт після налаштування середовища)


@pytest.fixture()
def client():
    wr.app.config["TESTING"] = True
    with wr.app.test_client() as c:
        yield c


@pytest.fixture()
def admin(client):
    """Клієнт, залогінений як адмін за замовчуванням (admin/admin)."""
    client.post("/login", data={"login": "admin", "password": "admin"})
    return client


def make_jury(admin_client, login="jury1", password="jurypass", name="Журі Один"):
    admin_client.post("/admin/user/save", data={
        "login": login, "password": password, "name": name, "role": "jury"})


def add_jury_to_contest(admin_client, contest, *logins):
    """Журі бачить і оцінює лише конкурси, до яких його додано (contest_jury)."""
    admin_client.post("/admin/contest/save", data={
        "id": contest["id"], "name": contest["name"], "template": contest["template"],
        "projects": "\n".join(contest["projects"]), "participants": "\n".join(contest["participants"]),
        "formula": contest["formula"], "start": contest["start"], "end": contest["end"],
        "jury": list(logins)})


def login_as(client, login, password):
    client.get("/logout")
    return client.post("/login", data={"login": login, "password": password})


def make_contest(admin_client, name="Тестовий конкурс",
                 formula="bytes / 1000 + quality * 10"):
    admin_client.post("/admin/contest/save", data={
        "id": "",
        "name": name,
        "projects": "https://uk.wikivoyage.org/wiki/Головна_сторінка\n"
                    "https://uk.wikiquote.org/wiki/Головна_сторінка",
        "participants": "Учасник1\nУчасник2\nUserThree",
        "formula": formula,
        "template": "https://uk.wikivoyage.org/wiki/Шаблон:Test_2026",
        "start": "2026-01-01",
        "end": "2026-12-31",
    })
    return wr_get_contests()[-1]


def wr_get_contests():
    with wr.app.app_context():
        return wr.get_contests()


# ─────────────────────────────────────────────────────────────
# 1. Формула ранжування
# ─────────────────────────────────────────────────────────────
class TestFormula:
    def test_simple_arithmetic(self):
        assert wr.eval_formula("2 + 2 * 2", {}) == 6

    def test_variables(self):
        v = {"bytes": 5000, "edits": 10, "articles": 4, "quality": 7.5}
        assert wr.eval_formula("bytes / 1000 + quality * 10", v) == 80.0

    def test_all_variables_available(self):
        v = {"bytes": 1, "edits": 2, "articles": 3, "quality": 4}
        assert wr.eval_formula("bytes + edits + articles + quality", v) == 10

    def test_parentheses_and_unary(self):
        v = {"bytes": 100, "quality": 2}
        assert wr.eval_formula("-(bytes) * (quality + 1)", v) == -300

    def test_power(self):
        assert wr.eval_formula("quality ** 2", {"quality": 3}) == 9

    def test_unknown_variable_rejected(self):
        assert wr.eval_formula("hacker_var + 1", {}) is None

    def test_code_injection_rejected(self):
        # спроби виконати довільний код мають повертати None
        assert wr.eval_formula("__import__('os').system('id')", {}) is None
        assert wr.eval_formula("().__class__", {}) is None
        assert wr.eval_formula("open('/etc/passwd')", {}) is None

    def test_syntax_error_rejected(self):
        assert wr.eval_formula("bytes +* 2", {"bytes": 1}) is None
        assert wr.eval_formula("", {}) is None

    def test_validate_formula(self):
        assert wr.validate_formula("bytes / 1000 + quality * 10")
        assert not wr.validate_formula("")
        assert not wr.validate_formula("import os")
        assert not wr.validate_formula("evil()")


# ─────────────────────────────────────────────────────────────
# 2. Авторизація та доступ
# ─────────────────────────────────────────────────────────────
class TestAuth:
    def test_index_requires_login(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_login_wrong_password(self, client):
        r = client.post("/login", data={"login": "admin", "password": "wrong"})
        assert r.status_code == 200  # залишаємось на сторінці входу

    def test_login_ok_and_logout(self, client):
        r = client.post("/login", data={"login": "admin", "password": "admin"})
        assert r.status_code == 302
        assert client.get("/").status_code == 200
        client.get("/logout")
        assert client.get("/").status_code == 302

    def test_default_admin_created(self):
        with wr.app.app_context():
            row = wr.db().execute(
                "SELECT * FROM users WHERE login='admin'").fetchone()
        assert row is not None
        assert row["role"] == "admin"

    def test_password_is_hashed(self):
        with wr.app.app_context():
            row = wr.db().execute(
                "SELECT pass FROM users WHERE login='admin'").fetchone()
        assert row["pass"] != "admin"
        assert row["pass"].startswith(("pbkdf2:", "scrypt:"))

    def test_jury_cannot_open_admin_routes(self, admin):
        make_jury(admin, "juryX", "pw1234")
        login_as(admin, "juryX", "pw1234")
        r = admin.post("/admin/contest/save", data={"name": "x"})
        assert r.status_code == 403
        r = admin.post("/admin/user/save", data={"login": "h", "password": "h"})
        assert r.status_code == 403

    def test_api_requires_login(self, client):
        client.get("/logout")
        r = client.post("/api/score", json={"contest": "c1",
                                            "participant": "U", "value": 5})
        assert r.status_code == 302


# ─────────────────────────────────────────────────────────────
# 3. Керування користувачами
# ─────────────────────────────────────────────────────────────
class TestUsers:
    def test_admin_creates_jury(self, admin):
        make_jury(admin, "jury_new", "secret1", "Нове Журі")
        with wr.app.app_context():
            row = wr.db().execute(
                "SELECT * FROM users WHERE login='jury_new'").fetchone()
        assert row["name"] == "Нове Журі"
        assert row["role"] == "jury"
        # створене журі може увійти
        r = login_as(admin, "jury_new", "secret1")
        assert r.status_code == 302

    def test_duplicate_login_ignored(self, admin):
        make_jury(admin, "dup", "one11")
        make_jury(admin, "dup", "two22", "Інший")
        with wr.app.app_context():
            rows = wr.db().execute(
                "SELECT * FROM users WHERE login='dup'").fetchall()
        assert len(rows) == 1

    def test_admin_deletes_user_but_not_self(self, admin):
        make_jury(admin, "todelete", "pw1234")
        admin.post("/admin/user/delete", data={"login": "todelete"})
        admin.post("/admin/user/delete", data={"login": "admin"})
        with wr.app.app_context():
            assert wr.db().execute(
                "SELECT * FROM users WHERE login='todelete'").fetchone() is None
            assert wr.db().execute(
                "SELECT * FROM users WHERE login='admin'").fetchone() is not None

    def test_change_password(self, admin):
        make_jury(admin, "chg", "oldpass")
        login_as(admin, "chg", "oldpass")
        admin.post("/password", data={"p1": "newpass", "p2": "newpass"})
        assert login_as(admin, "chg", "newpass").status_code == 302
        # старий пароль більше не діє
        r = login_as(admin, "chg", "oldpass")
        assert r.status_code == 200

    def test_change_password_mismatch_rejected(self, admin):
        make_jury(admin, "mm", "startpw")
        login_as(admin, "mm", "startpw")
        admin.post("/password", data={"p1": "aaaa", "p2": "bbbb"})
        assert login_as(admin, "mm", "startpw").status_code == 302


# ─────────────────────────────────────────────────────────────
# 4. Конкурси
# ─────────────────────────────────────────────────────────────
class TestContests:
    def test_create_contest(self, admin):
        c = make_contest(admin, "Вікімандри-2026")
        assert c["name"] == "Вікімандри-2026"
        assert len(c["projects"]) == 2
        assert c["participants"] == ["Учасник1", "Учасник2", "UserThree"]
        assert c["template"].endswith("Test_2026")

    def test_invalid_formula_replaced_with_default(self, admin):
        c = make_contest(admin, "Зламаний", formula="__import__('os')")
        assert c["formula"] == ""  # некоректну формулу не зберігаємо; порожня = формула не використовується

    def test_edit_contest(self, admin):
        c = make_contest(admin, "До редагування")
        admin.post("/admin/contest/save", data={
            "id": c["id"], "name": "Після редагування",
            "projects": "https://en.wikivoyage.org/wiki/Main_Page",
            "participants": "OnlyOne", "formula": "bytes",
            "template": "", "start": "", "end": ""})
        upd = next(x for x in wr_get_contests() if x["id"] == c["id"])
        assert upd["name"] == "Після редагування"
        assert upd["participants"] == ["OnlyOne"]
        assert upd["formula"] == "bytes"

    def test_delete_contest_cascades(self, admin):
        c = make_contest(admin, "На видалення")
        cid = c["id"]
        admin.post("/api/score", json={"contest": cid,
                                       "participant": "Учасник1", "value": 5})
        admin.post("/api/comment", json={"contest": cid,
                                         "participant": "Учасник1", "text": "тест"})
        admin.post("/admin/contest/delete", data={"id": cid})
        with wr.app.app_context():
            for tbl, col in (("contests", "id"), ("scores", "contest_id"),
                             ("comments", "contest_id"), ("results", "contest_id")):
                rows = wr.db().execute(
                    f"SELECT * FROM {tbl} WHERE {col}=?", (cid,)).fetchall()
                assert rows == [], f"{tbl} не очищено"


# ─────────────────────────────────────────────────────────────
# 5. Оцінки, коментарі, результати
# ─────────────────────────────────────────────────────────────
class TestAssessment:
    def test_score_save_update_delete(self, admin):
        c = make_contest(admin, "Оцінки")
        cid = c["id"]
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": 7})
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": 9})
        st = admin.get(f"/api/state/{cid}").get_json()
        assert st["assess"]["Учасник1"]["Стаття А"]["scores"]["admin"] == 9
        # value=null видаляє оцінку
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": None})
        st = admin.get(f"/api/state/{cid}").get_json()
        assert "admin" not in st["assess"].get("Учасник1", {}).get("Стаття А", {}).get("scores", {})

    def test_scores_independent_per_article(self, admin):
        c = make_contest(admin, "Незалежні статті")
        cid = c["id"]
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": 5})
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття Б", "value": 9})
        st = admin.get(f"/api/state/{cid}").get_json()
        assess = st["assess"]["Учасник1"]
        assert assess["Стаття А"]["scores"]["admin"] == 5
        assert assess["Стаття Б"]["scores"]["admin"] == 9

    def test_average_of_two_juries(self, admin):
        c = make_contest(admin, "Середнє")
        cid = c["id"]
        make_jury(admin, "j_avg", "pw1234", "Друге Журі")
        add_jury_to_contest(admin, c, "j_avg")
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник2",
                                       "article": "Стаття А", "value": 6})
        login_as(admin, "j_avg", "pw1234")
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник2",
                                       "article": "Стаття А", "value": 10})
        st = admin.get(f"/api/state/{cid}").get_json()
        scores = st["assess"]["Учасник2"]["Стаття А"]["scores"]
        assert sorted(scores.values()) == [6, 10]

    def test_quality_is_mean_of_article_means(self, admin):
        # Учасник1: стаття А середня 6 (журі 4 і 8), стаття Б середня 10 (одне журі)
        # якість = (6 + 10) / 2 = 8, а не пласке середнє (4+8+10)/3
        c = make_contest(admin, "Формула якості")
        cid = c["id"]
        make_jury(admin, "j2", "pw1234", "Журі 2")
        add_jury_to_contest(admin, c, "j2")
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": 4})
        login_as(admin, "j2", "pw1234")
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": 8})
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття Б", "value": 10})
        with wr.app.app_context():
            assess = wr.get_assess(cid)
        quality, n = wr.article_quality(assess["Учасник1"])
        assert quality == 8
        assert n == 3

    def test_comment_saved_with_author_and_date(self, admin):
        c = make_contest(admin, "Коментарі")
        cid = c["id"]
        admin.post("/api/comment", json={
            "contest": cid, "participant": "UserThree", "article": "Стаття А",
            "text": "Гарні описи маршрутів"})
        st = admin.get(f"/api/state/{cid}").get_json()
        cm = st["assess"]["UserThree"]["Стаття А"]["comments"][0]
        assert cm["text"] == "Гарні описи маршрутів"
        assert cm["jury"] == "Admin"
        assert len(cm["date"]) == 16  # YYYY-MM-DD HH:MM

    def test_comments_independent_per_article(self, admin):
        c = make_contest(admin, "Коментарі по статтях")
        cid = c["id"]
        admin.post("/api/comment", json={"contest": cid, "participant": "UserThree",
                                         "article": "Стаття А", "text": "про статтю А"})
        admin.post("/api/comment", json={"contest": cid, "participant": "UserThree",
                                         "article": "Стаття Б", "text": "про статтю Б"})
        st = admin.get(f"/api/state/{cid}").get_json()
        assess = st["assess"]["UserThree"]
        assert assess["Стаття А"]["comments"][0]["text"] == "про статтю А"
        assert assess["Стаття Б"]["comments"][0]["text"] == "про статтю Б"

    def test_comment_length_limited(self, admin):
        c = make_contest(admin, "Довгий")
        admin.post("/api/comment", json={
            "contest": c["id"], "participant": "Учасник1",
            "article": "Стаття А", "text": "x" * 5000})
        st = admin.get(f"/api/state/{c['id']}").get_json()
        assert len(st["assess"]["Учасник1"]["Стаття А"]["comments"][0]["text"]) == 2000

    def test_results_roundtrip(self, admin):
        c = make_contest(admin, "Результати")
        cid = c["id"]
        payload = {"Учасник1": {"perProject": {}, "bytes": 12345,
                                "edits": 10, "articles": 3}}
        admin.post(f"/api/results/{cid}", json=payload)
        with wr.app.app_context():
            row = wr.db().execute(
                "SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
        assert json.loads(row["data"]) == payload


# ─────────────────────────────────────────────────────────────
# 6. Експорт CSV
# ─────────────────────────────────────────────────────────────
class TestExport:
    def _prepare(self, admin):
        c = make_contest(admin, "CSV Contest", formula="bytes + quality * 100")
        cid = c["id"]
        admin.post(f"/api/results/{cid}", json={
            "Учасник1": {"perProject": {
                "uk.wikivoyage.org": {"bytes": 800, "edits": 4, "articles": 2},
                "uk.wikiquote.org": {"bytes": 200, "edits": 1, "articles": 1}},
                "bytes": 1000, "edits": 5, "articles": 3},
            "Учасник2": {"perProject": {}, "bytes": 50, "edits": 1, "articles": 1},
        })
        admin.post("/api/score", json={"contest": cid, "participant": "Учасник1",
                                       "article": "Стаття А", "value": 8})
        admin.post("/api/comment", json={"contest": cid, "participant": "Учасник1",
                                         "article": "Стаття А",
                                         "text": "чудова робота"})
        return cid

    def test_quantitative_csv(self, admin):
        cid = self._prepare(admin)
        r = admin.get(f"/export/{cid}.csv?quant=1&qual=0")
        assert r.status_code == 200
        assert "text/csv" in r.content_type
        body = r.get_data(as_text=True)
        assert "Учасник1" in body and "1000" in body
        assert "uk.wikivoyage.org" in body
        # сортування за байтами: Учасник1 вище
        assert body.index("Учасник1") < body.index("Учасник2")

    def test_qualitative_csv_has_score_and_comments(self, admin):
        cid = self._prepare(admin)
        r = admin.get(f"/export/{cid}.csv?quant=1&qual=1")
        body = r.get_data(as_text=True)
        # bytes + quality*100 = 1000 + 800 = 1800
        assert "1800" in body
        assert "чудова робота" in body
        assert "Admin" in body

    def test_csv_requires_login(self, admin):
        cid = self._prepare(admin)
        admin.get("/logout")
        assert admin.get(f"/export/{cid}.csv").status_code == 302

    def test_missing_contest_404(self, admin):
        assert admin.get("/export/nope.csv").status_code == 404

    def test_bom_for_excel(self, admin):
        cid = self._prepare(admin)
        raw = admin.get(f"/export/{cid}.csv").get_data()
        assert raw.startswith("\ufeff".encode("utf-8"))


# ─────────────────────────────────────────────────────────────
# 7. Інтерфейс та i18n
# ─────────────────────────────────────────────────────────────
class TestUI:
    def test_login_page_ukrainian_default(self, client):
        client.get("/logout")
        r = client.get("/login")
        assert "ВікіРанг" in r.get_data(as_text=True)

    def test_login_page_english(self, client):
        client.get("/logout")
        r = client.get("/login?lang=en")
        assert "WikiRank" in r.get_data(as_text=True)

    def test_lang_persists_in_session(self, admin):
        admin.get("/?lang=en")
        r = admin.get("/")
        assert "Assessment" in r.get_data(as_text=True)
        admin.get("/?lang=uk")
        r = admin.get("/")
        assert "Оцінювання" in r.get_data(as_text=True)

    def test_main_page_contains_contest_data(self, admin):
        make_contest(admin, "Видимий конкурс")
        r = admin.get("/")
        body = r.get_data(as_text=True)
        assert "Видимий конкурс" in body

    def test_admin_view_hidden_for_jury(self, admin):
        make_jury(admin, "jury_ui", "pw1234")
        login_as(admin, "jury_ui", "pw1234")
        body = admin.get("/?view=admin").get_data(as_text=True)
        # журі перенаправляється на оцінювання, форм адміна немає
        assert 'action="/admin/contest/save"' not in body


def teardown_module(_):
    try:
        os.unlink(_db_path)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────
# Мінімум байтів для доповнень + рейтинг статей за балами журі
# ─────────────────────────────────────────────────────────────
class TestMinExpandAndRanking:
    LIST = [
        {"title": "Нова мала", "bytes": 300, "created": True},
        {"title": "Доп велика", "bytes": 5000, "created": False},
        {"title": "Доп мала", "bytes": 200, "created": False},
    ]

    def test_in_scope_drops_small_expansions_only(self):
        small_exp, big_exp, small_new = self.LIST[2], self.LIST[1], self.LIST[0]
        assert wr.in_scope(small_exp, "all", (), 0)
        assert not wr.in_scope(small_exp, "all", (), 500)
        assert wr.in_scope(big_exp, "all", (), 500)
        assert wr.in_scope(small_new, "all", (), 500)        # нові не відсіюються
        assert not wr.in_scope(small_exp, "expanded", (), 500)

    def test_scope_titles_with_threshold(self):
        r = {"perProject": {"h": {"list": self.LIST}}}
        assert wr.scope_titles(r, "all", (), 0) is None
        assert wr.scope_titles(r, "all", (), 500) == {"Нова мала", "Доп велика"}

    def test_parse_min_expand(self):
        assert wr.parse_min_expand("1500") == 1500
        assert wr.parse_min_expand("-5") == 0
        assert wr.parse_min_expand("abc") == 0
        assert wr.parse_min_expand("") == 0

    def test_kind_quality_separates_created_and_expanded(self):
        r = {"perProject": {"h": {"list": self.LIST}}}
        a = {"Нова мала": {"scores": {"j1": 8, "j2": 6}},
             "Доп велика": {"scores": {"j1": 4}},
             "Доп мала": {"scores": {"j1": 10}}}
        kq = wr.kind_quality(a, r, "all", (), 0)
        assert kq["created"] == (7.0, 1)
        assert kq["expanded"] == (7.0, 2)               # (4 + 10) / 2
        kq = wr.kind_quality(a, r, "all", (), 500)           # «Доп мала» відсіяна
        assert kq["expanded"] == (4.0, 1)

    def test_save_min_expand_and_distribute_skips_small(self, admin):
        make_jury(admin, "jx", "jurypass", "Журі Х")
        admin.post("/admin/contest/save", data={
            "id": "cme", "name": "Мін доповнення", "participants": "U1",
            "projects": "https://uk.wikivoyage.org/wiki/X", "jury": ["jx"],
            "min_expand": "500", "review_scope": "all"})
        with wr.app.app_context():
            row = wr.db().execute("SELECT min_expand FROM contests WHERE id='cme'").fetchone()
            assert row["min_expand"] == 500
        admin.post("/api/results/cme", json={"U1": {
            "bytes": 5500, "edits": 3, "articles": 3,
            "perProject": {"uk.wikivoyage.org": {"bytes": 5500, "list": self.LIST}}}})
        resp = admin.post("/api/distribute/cme", json={"per_article": 1})
        assert resp.get_json()["items"] == 2
        with wr.app.app_context():
            arts = {r["article"] for r in wr.db().execute(
                "SELECT article FROM article_assignments WHERE contest_id='cme'")}
        assert arts == {"Нова мала", "Доп велика"}

    def test_export_has_split_score_columns_and_sort(self, admin):
        admin.post("/admin/contest/save", data={
            "id": "cex", "name": "Експорт", "participants": "A\nB",
            "projects": "https://uk.wikivoyage.org/wiki/X"})
        mk = lambda b: {"bytes": b, "edits": 1, "articles": 2, "perProject": {"uk.wikivoyage.org": {
            "bytes": b, "list": [{"title": f"n{b}", "bytes": b, "created": True},
                                 {"title": f"e{b}", "bytes": b, "created": False}]}}}
        admin.post("/api/results/cex", json={"A": mk(2000), "B": mk(3000)})
        with wr.app.app_context():
            con = wr.db()
            for p, art, v in (("A", "n2000", 9), ("A", "e2000", 3), ("B", "n3000", 2), ("B", "e3000", 8)):
                con.execute("INSERT INTO article_scores VALUES('cex',?,?,?,?)", (p, art, "j", v))
            con.commit()
        body = admin.get("/export/cex.csv?quant=1&qual=1&sort=qc").data.decode("utf-8-sig")
        lines = body.strip().splitlines()
        assert "Бал: нові" in lines[0] and "Бал: доповнені" in lines[0]
        assert lines[1].split(",")[1] == "A"             # A має вищий бал за нові
        body = admin.get("/export/cex.csv?quant=1&qual=1&sort=qe").data.decode("utf-8-sig")
        assert body.strip().splitlines()[1].split(",")[1] == "B"


class TestMinExpandPreview:
    def test_preview_lists_scored_expansions_that_would_drop(self, admin):
        admin.post("/admin/contest/save", data={
            "id": "cpv", "name": "Прев'ю", "participants": "P",
            "projects": "https://uk.wikivoyage.org/wiki/X"})
        lst = [{"title": "Нова", "bytes": 50, "created": True},
               {"title": "Мала доп", "bytes": 200, "created": False},
               {"title": "Велика доп", "bytes": 5000, "created": False},
               {"title": "Мала без оцінки", "bytes": 100, "created": False}]
        admin.post("/api/results/cpv", json={"P": {"bytes": 5350, "edits": 1, "articles": 4,
                   "perProject": {"h": {"bytes": 5350, "list": lst}}}})
        for a in ("Нова", "Мала доп", "Велика доп"):
            admin.post("/api/score", json={"contest": "cpv", "participant": "P", "article": a, "value": 7})
        d = admin.get("/api/min_expand_preview/cpv?value=1000").get_json()
        assert d["count"] == 1 and d["items"][0]["article"] == "Мала доп"   # нова й без оцінки не рахуються
        assert admin.get("/api/min_expand_preview/cpv?value=0").get_json()["count"] == 0

    def test_preview_admin_only(self, admin):
        make_jury(admin, "jp", "jurypass", "Журі П")
        login_as(admin, "jp", "jurypass")
        assert admin.get("/api/min_expand_preview/cpv?value=10").status_code == 403


class TestMinCreated:
    def test_in_scope_min_new(self):
        new_small = {"title": "n", "bytes": 3000, "created": True}
        new_big = {"title": "N", "bytes": 4000, "created": True}
        exp_small = {"title": "e", "bytes": 3000, "created": False}
        assert not wr.in_scope(new_small, "all", (), 0, 3500)
        assert wr.in_scope(new_big, "all", (), 0, 3500)
        assert wr.in_scope(exp_small, "all", (), 0, 3500)          # поріг нових не діє на доповнення
        assert not wr.in_scope(exp_small, "all", (), 3500, 0)
        assert wr.scope_titles({"perProject": {"h": {"list": [new_small, new_big]}}}, "all", (), 0, 3500) == {"N"}

    def test_save_and_distribute_with_min_created(self, admin):
        make_jury(admin, "jm", "jurypass", "Журі М")
        admin.post("/admin/contest/save", data={
            "id": "cmc", "name": "Мін нова", "participants": "U", "jury": ["jm"],
            "projects": "https://uk.wikivoyage.org/wiki/X", "min_created": "3500", "min_expand": "3500"})
        lst = [{"title": "Нова мала", "bytes": 3000, "created": True},
               {"title": "Нова ок", "bytes": 4000, "created": True},
               {"title": "Доп мала", "bytes": 3000, "created": False},
               {"title": "Доп ок", "bytes": 3600, "created": False}]
        admin.post("/api/results/cmc", json={"U": {"bytes": 13600, "edits": 4, "articles": 4,
                   "perProject": {"h": {"bytes": 13600, "list": lst}}}})
        assert admin.post("/api/distribute/cmc", json={}).get_json()["items"] == 2
        admin.post("/api/score", json={"contest": "cmc", "participant": "U", "article": "Нова ок", "value": 5})
        d = admin.get("/api/min_expand_preview/cmc?value=3500&value_new=4500").get_json()
        assert d["count"] == 1 and d["items"][0]["article"] == "Нова ок"   # 4000 < 4500
        assert admin.get("/api/min_expand_preview/cmc?value=3500&value_new=3500").get_json()["count"] == 0
        d = admin.get("/api/min_expand_preview/cmc?value=3500&value_new=3500&value_bytes=99999").get_json()
        assert d["count"] == 1    # поріг внеску учасника теж відсіює


class TestMinBytesConfigurable:
    def test_default_is_off_and_configurable(self, admin):
        make_jury(admin, "jb", "jurypass", "Журі Б")
        for cid, mb in (("cb0", ""), ("cb1", "5000")):
            admin.post("/admin/contest/save", data={
                "id": cid, "name": cid, "participants": "S", "jury": ["jb"],
                "projects": "https://uk.wikivoyage.org/wiki/X", "min_bytes": mb})
            admin.post(f"/api/results/{cid}", json={"S": {"bytes": 2000, "edits": 1, "articles": 1,
                       "perProject": {"h": {"bytes": 2000, "list": [{"title": "T", "bytes": 2000, "created": True}]}}}})
        assert admin.post("/api/distribute/cb0", json={}).get_json().get("items") == 1   # без порога
        assert admin.post("/api/distribute/cb1", json={}).status_code == 400            # 2000 < 5000


# ── Доступність: axe-core у реальному браузері (пропускається, якщо немає playwright/axe/Chrome) ──
class TestAccessibility:
    AXE = os.environ.get("AXE_JS", "/home/claude/a11y/node_modules/axe-core/axe.min.js")

    def _run(self, client_app, urls):
        sync = pytest.importorskip("playwright.sync_api")
        if not os.path.exists(self.AXE):
            pytest.skip("axe-core не знайдено (AXE_JS)")
        import threading
        from werkzeug.serving import make_server
        srv = make_server("127.0.0.1", 5077, wr.app)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        out = {}
        try:
            with sync.sync_playwright() as p:
                chrome = next((c for c in ("/usr/bin/google-chrome", "/usr/bin/chromium") if os.path.exists(c)), None)
                if not chrome:
                    pytest.skip("Chrome не знайдено")
                br = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
                pg = br.new_page()
                pg.goto("http://127.0.0.1:5077/login")
                pg.fill("input[name=login]", "admin")
                pg.fill("input[name=password]", "admin")
                pg.click("button.btn-p")
                axe = open(self.AXE).read()
                for u in urls:
                    pg.goto("http://127.0.0.1:5077" + u)
                    pg.wait_for_timeout(400)
                    pg.evaluate(axe)
                    r = pg.evaluate("axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}})")
                    out[u] = [(v["id"], len(v["nodes"])) for v in r["violations"]]
                br.close()
        finally:
            srv.shutdown()
        return out

    def test_axe_no_violations(self, client):
        # адмін за замовчуванням створюється в init_db (admin/admin у тестовому середовищі)
        res = self._run(client, ["/", "/?view=admin"])
        assert all(not v for v in res.values()), res
