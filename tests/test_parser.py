from app.services.parser import extract_relationships


def test_dependency_near_link():
    html = '<div id="tab-description"><h2>Example</h2><p>This mod <strong>depends on</strong> <a href="https://mods.vintagestory.at/show/mod/123">Common Library</a>.</p></div>'
    rel = extract_relationships(html, [{"name": "Common Library", "mod_id": "commonlib", "mod_db_url": "https://mods.vintagestory.at/show/mod/123"}])
    assert rel and rel[0].relation_type == "dependency"
    assert rel[0].url.endswith("/123")
    assert rel[0].confidence >= 0.90


def test_multiple_dependency_links_are_kept():
    html = '''<div id="tab-description"><p><strong>Requires:</strong> <a href="https://mods.vintagestory.at/show/mod/55292">Expanded Library</a> 0.8.4 or later and <a href="https://mods.vintagestory.at/show/mod/55293">Pipes and Power Expanded</a> 0.7.1 or later.</p></div>'''
    rel = extract_relationships(html, [])
    urls = {r.url for r in rel if r.relation_type == "dependency"}
    assert "https://mods.vintagestory.at/show/mod/55292" in urls
    assert "https://mods.vintagestory.at/show/mod/55293" in urls


def test_comments_release_download_and_changelog_are_ignored():
    html = '''
    <html><head><meta property="og:title" content="Immersive Woodworking"></head><body>
      <h1>Disclaimer</h1>
      <div id="tab-description">
        <h2>Immersive Woodworking</h2>
        <p>Recommended download (for Vintage Story 1.22.2 - 1.22.7): download now.</p>
        <p>Requires <a href="https://mods.vintagestory.at/show/mod/123">Real Dependency</a>.</p>
        <h3>Compatible</h3>
        <ul><li><a href="https://mods.vintagestory.at/toolsmith">Toolsmith</a></li></ul>
        <h3>Incompatible</h3>
        <ul><li><a href="https://mods.vintagestory.at/show/mod/30143">VSroofing</a></li></ul>
        <h3>1.1.0 - 2024-08-29</h3>
        <p>This changelog says it requires <a href="https://mods.vintagestory.at/show/mod/999">Fake Changelog Mod</a>.</p>
      </div>
      <table><tr><th>Mod Version</th><th>Mod Identifier</th><th>For Game version</th><th>Download</th></tr></table>
      <h2>31 Comments</h2><p>Requires <a href="https://mods.vintagestory.at/show/mod/888">Fake Comment Mod</a>.</p>
    </body></html>'''
    rel = extract_relationships(html, [])
    assert any(r.relation_type == "dependency" and r.url.endswith("/123") for r in rel)
    assert any(r.relation_type == "compatible" and r.target_name == "Toolsmith" for r in rel)
    assert any(r.relation_type == "incompatible" and r.target_name == "VSroofing" for r in rel)
    assert not any("999" in (r.url or "") for r in rel)
    assert not any("888" in (r.url or "") for r in rel)
    assert not any("recommended download" in r.evidence.casefold() for r in rel)


def test_no_false_positive_from_generic_words_in_long_text():
    html = '''<div id="tab-description"><p>A server restart is needed to apply the setting. The project is more than just JSON files and does not require programming knowledge.</p><p>Dependency is discussed here only as a concept and there is no mod name or link.</p></div>'''
    rel = extract_relationships(html, [{"name":"JSON Utils", "mod_id":"jsonutils"}])
    assert rel == []


def test_recommended_links_are_optional_while_require_links_are_dependencies():
    html = '''<div id="tab-description">
      <p><a href="https://mods.vintagestory.at/ithaniabackpacks">Ithania Backpacks</a> recommend</p>
      <p><a href="https://mods.vintagestory.at/show/mod/36679">Primitive Backpacks</a> recommended</p>
      <p><a href="https://mods.vintagestory.at/butchering">Butchering</a> require</p>
      <p><a href="https://mods.vintagestory.at/specializedbagsrevived">Specialized Bags Revived</a> requires</p>
    </div>'''
    rel = extract_relationships(html, [])
    assert any(r.relation_type == "optional_dependency" and "ithaniabackpacks" in r.url for r in rel)
    assert any(r.relation_type == "optional_dependency" and r.url.endswith("/36679") for r in rel)
    assert any(r.relation_type == "dependency" and r.url.endswith("/butchering") for r in rel)
    assert any(r.relation_type == "dependency" and r.url.endswith("/specializedbagsrevived") for r in rel)


def test_bare_mod_link_is_low_confidence_optional():
    html = '<div id="tab-description"><h2>See also</h2><p><a href="https://mods.vintagestory.at/show/mod/123">Some Other Mod</a></p></div>'
    rel = extract_relationships(html, [])
    assert rel and rel[0].relation_type == "optional_dependency"
    assert rel[0].confidence < 0.90


def test_required_by_is_separate_relation():
    html = '<div id="tab-description"><p>This mod is required by <a href="https://mods.vintagestory.at/show/mod/999">Another Mod</a>.</p></div>'
    rel = extract_relationships(html, [])
    assert any(r.relation_type == "required_by" for r in rel)
    assert not any(r.relation_type == "dependency" for r in rel)


def test_description_region_with_disclaimer_h1_still_uses_real_title_and_relation():
    html = '''<html><head><meta property="og:title" content="Some Mod"></head><body>
      <h1>Disclaimer</h1>
      <div id="tab-description">
        <h2>Some Mod</h2>
        <p>Requires: <a href="https://mods.vintagestory.at/show/mod/77">Real Library</a>.</p>
      </div>
      <div id="comments"><p>Requires <a href="https://mods.vintagestory.at/show/mod/88">Fake</a>.</p></div>
    </body></html>'''
    rel = extract_relationships(html, [])
    assert len(rel) == 1
    assert rel[0].relation_type == "dependency"
    assert rel[0].url.endswith("/77")
