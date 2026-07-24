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
        assert c["formula"] == "bytes / 1000 + quality * 10"

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
