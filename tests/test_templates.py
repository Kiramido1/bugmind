import pytest
from jinja2.exceptions import SecurityError, TemplateError

from bugmind.models import BugAnalysis
from bugmind.templates import ReportTemplate, TemplateEngine, TemplateNotFound

SECTIONS = ["**Bug Title:**", "**Environment & Endpoint Details:**", "**Page URL:**", "**API Endpoint:**",
            "**Request Method & Status Code:**", "**Description:**", "**Steps to Reproduce:**",
            "**Actual Result:**", "**Expected Result:**"]


def test_default_template_has_exact_sections_in_order():
    eng = TemplateEngine()
    tpl = eng.resolve("default/professional")
    out = eng.render(tpl, BugAnalysis(title="[API] t", api_endpoints=["/a", "/b"], http_method="GET",
                                      actual_status_code="500", description="line1\nline2",
                                      steps_to_reproduce=["s1", "s2"], actual_result=["r"], expected_result=["e"]))
    positions = [out.index(s) for s in SECTIONS]
    assert positions == sorted(positions)
    assert "**Page URL:** N/A" in out  # missing info never invented
    assert "**API Endpoint:** /a, /b" in out and "GET — 500" in out
    assert "  1. s1\n  2. s2" in out and "  line2" in out
    assert "Do not include proposed code fixes" in tpl.guidance


def test_missing_fields_render_na():
    eng = TemplateEngine()
    out = eng.render(eng.resolve("default/professional"), BugAnalysis(title="t"))
    assert "N/A — N/A" in out and "  1. N/A" in out


def test_arabic_input_content_is_preserved_verbatim():
    eng = TemplateEngine()
    out = eng.render(eng.resolve("default/professional"), BugAnalysis(title="t", evidence=["الخطأ ٥٠٠"],
                                                                       description="Quoted: «القيمة فارغة»"))
    assert "«القيمة فارغة»" in out


def test_user_templates_override_builtins_and_project_templates(tmp_path):
    user = tmp_path / "templates"
    (user / "default").mkdir(parents=True)
    (user / "default" / "professional_en.md").write_text("---\nname: Mine\n---\nTITLE={{ title }}\n")
    eng = TemplateEngine(user)
    tpl = eng.resolve("default/professional")
    assert tpl.name == "Mine" and eng.render(tpl, BugAnalysis(title="x")) == "TITLE=x\n"
    proj = eng.resolve("projects/example_backend/custom_bug_report")
    assert "| Severity |" in eng.render(proj, BugAnalysis(title="x"))
    ids = [t[0] for t in eng.list()]
    assert "default/professional" in ids and "projects/example_backend/custom_bug_report" in ids


def test_traversal_and_unknown_templates_rejected():
    eng = TemplateEngine()
    with pytest.raises(ValueError):
        eng.resolve("../../etc/passwd")
    with pytest.raises(TemplateNotFound):
        eng.resolve("default/nope")


def test_templates_are_sandboxed(tmp_path):
    evil = ReportTemplate(id="x", language="en", path=tmp_path / "x.md", name="x", guidance="",
                          body="{{ title.__class__.__mro__[1].__subclasses__() }}")
    with pytest.raises((SecurityError, TemplateError)):
        TemplateEngine().render(evil, BugAnalysis(title="t"))


def test_save_validates_and_never_touches_builtins(tmp_path):
    from bugmind.templates import BUILTIN_DIR, TemplateValidationError
    eng = TemplateEngine(tmp_path / "user")
    builtin = BUILTIN_DIR / "default" / "professional_en.md"
    before = builtin.read_text()
    text = before.replace("**Bug Title:**", "**Title:**")
    path = eng.save_user_template("default/professional", text)
    assert path == tmp_path / "user" / "default" / "professional_en.md"
    assert builtin.read_text() == before  # built-in untouched
    tpl = eng.resolve("default/professional")
    assert eng.is_user_template(tpl) and "**Title:**" in eng.render(tpl, BugAnalysis(title="x"))
    for bad in ("{{ nope }}", "{% if title %}x", "---\nname: [\n---\nbody", "---\nname: x\n---\n  "):
        with pytest.raises(TemplateValidationError):
            eng.save_user_template("mine/bad", bad)
    assert not (tmp_path / "user" / "mine").exists()  # nothing written on failure
    with pytest.raises(ValueError):
        eng.save_user_template("../escape", "x {{ title }}")
    # reset: deleting the copy falls back to the built-in
    assert eng.delete_user_template("default/professional")
    assert not eng.is_user_template(eng.resolve("default/professional"))
    assert eng.has_builtin("default/professional") and not eng.has_builtin("mine/new")


async def test_tui_template_editor_saves_user_copy(tmp_path):
    from textual.widgets import TextArea

    from bugmind.config import config_dir, load_config
    from bugmind.tui.app import BugMindApp

    app = BugMindApp(load_config())
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause(0.3)
        if app.screen.__class__.__name__ == "ProviderSelectScreen":
            await pilot.press("1")
        app.action_show("templates")
        await pilot.pause(0.3)
        await pilot.click("#tpl-edit")
        await pilot.pause(0.3)
        view = app.query_one("#templates")
        assert view.editing_id == "default/professional"
        editor = app.query_one("#tpl-editor", TextArea)
        editor.move_cursor(editor.document.end)
        editor.insert("\n- **Severity:** {{ severity }}\n")
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        saved = config_dir() / "templates" / "default" / "professional_en.md"
        assert saved.exists() and "**Severity:** {{ severity }}" in saved.read_text()
        assert view.editing_id is None
        # invalid edits are refused
        await pilot.click("#tpl-edit")
        await pilot.pause(0.2)
        app.query_one("#tpl-editor", TextArea).insert("{{ broken")
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        assert view.editing_id is not None and "{{ broken" not in saved.read_text()
